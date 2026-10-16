"""Synchronization strategies: dual-write, transactional outbox, polling.

All strategies upsert Elasticsearch documents with the deterministic id
`order-<order_id>`, so retries and replays are idempotent. MongoDB is never
part of the order synchronization path.

Strategy summary (see NOTES.md for the full comparison):
- dual_write: index ES right after the PG commit. On failure, an outbox row is
  written in a *separate* PG transaction and a repair loop (every 10s) drains it.
- outbox: the outbox row is written *inside* the PG order transaction
  (atomic). A worker polls pending rows every OUTBOX_POLL_SECONDS, indexes to
  ES, and marks them done. Failures back off exponentially (2^attempts s) and
  become 'failed' after OUTBOX_MAX_ATTEMPTS attempts (10).
- polling: a worker every POLLING_INTERVAL_SECONDS selects orders with
  updated_at > watermark (stored in sync_state), upserts them, and advances the
  watermark. Survives ES outages (watermark simply doesn't advance).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..core.logging import get_logger
from ..repositories import orders_repo, search_repo

log = get_logger(__name__)

REPAIR_LOOP_SECONDS = 10
OUTBOX_BATCH_SIZE = 50
POLLING_BATCH_SIZE = 500


async def push_order_doc(es: Any, index: str, doc: Dict[str, Any]) -> None:
    """Upsert one order document. Raises on ES failure (caller handles)."""
    if es is None:
        raise ConnectionError("elasticsearch client unavailable")
    await search_repo.upsert_order_doc(es, index, doc)


async def process_outbox_once(
    session_factory: async_sessionmaker[AsyncSession],
    es: Any,
    index: str,
    settings: Settings,
) -> Dict[str, int]:
    """Drain due outbox rows once. Returns {"processed","failed"} counts."""
    processed, failed = 0, 0
    async with session_factory() as session:
        batch = await orders_repo.claim_outbox_batch(session, OUTBOX_BATCH_SIZE)
    for row in batch:
        try:
            await push_order_doc(es, index, row.payload)
            async with session_factory() as session:
                await orders_repo.mark_outbox_done(session, row.id)
            processed += 1
        except Exception as exc:
            log.warning(
                "outbox event failed", extra={"outbox_id": row.id, "error": str(exc)}
            )
            async with session_factory() as session:
                await orders_repo.mark_outbox_error(
                    session, row.id, str(exc), max_attempts=settings.OUTBOX_MAX_ATTEMPTS
                )
            failed += 1
    return {"processed": processed, "failed": failed}


async def run_outbox_loop(
    session_factory: async_sessionmaker[AsyncSession],
    es_factory: Callable[[], Any],
    index: str,
    settings: Settings,
    interval_seconds: int,
    stop: asyncio.Event,
) -> None:
    """Background loop: keep draining the outbox (worker or repair loop)."""
    log.info("outbox loop started", extra={"interval": interval_seconds})
    while not stop.is_set():
        try:
            es = es_factory()
            result = await process_outbox_once(session_factory, es, index, settings)
            if result["processed"] or result["failed"]:
                log.info("outbox cycle", extra=result)
        except Exception as exc:
            log.warning("outbox loop cycle failed", extra={"error": str(exc)})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
    log.info("outbox loop stopped")


async def run_polling_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    es: Any,
    index: str,
) -> Dict[str, Any]:
    """One polling pass: upsert orders newer than the watermark, advance it."""
    async with session_factory() as session:
        watermark = await orders_repo.get_polling_watermark(session)
    rows = await _fetch_since(session_factory, watermark)
    if not rows:
        return {"synced": 0, "watermark": watermark.isoformat() if watermark else None}

    max_updated = watermark
    synced = 0
    for entry in rows:
        doc = search_repo.build_order_doc(entry["order"], entry["user"], entry["items"])
        await push_order_doc(es, index, doc)  # raises -> watermark not advanced
        synced += 1
        if max_updated is None or entry["order"].updated_at > max_updated:
            max_updated = entry["order"].updated_at

    async with session_factory() as session:
        await orders_repo.set_polling_watermark(session, max_updated)
    log.info("polling cycle", extra={"synced": synced})
    return {"synced": synced, "watermark": max_updated.isoformat()}


async def _fetch_since(
    session_factory: async_sessionmaker[AsyncSession],
    watermark: Optional[datetime],
) -> List[Dict[str, Any]]:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    async with session_factory() as session:
        return await orders_repo.fetch_orders_for_sync(
            session, updated_after=watermark or epoch, limit=POLLING_BATCH_SIZE
        )


async def run_polling_loop(
    session_factory: async_sessionmaker[AsyncSession],
    es_factory: Callable[[], Any],
    index: str,
    interval_seconds: int,
    stop: asyncio.Event,
) -> None:
    log.info("polling loop started", extra={"interval": interval_seconds})
    while not stop.is_set():
        try:
            es = es_factory()
            if es is None:
                raise ConnectionError("elasticsearch client unavailable")
            await run_polling_cycle(session_factory, es, index)
        except Exception as exc:
            log.warning("polling cycle failed", extra={"error": str(exc)})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
    log.info("polling loop stopped")


async def reindex_all(
    session_factory: async_sessionmaker[AsyncSession],
    es: Any,
    index: str,
) -> int:
    """Full rebuild of the ES index from PostgreSQL (snapshots only, never Mongo)."""
    if es is None:
        raise ConnectionError("elasticsearch client unavailable")
    await search_repo.ensure_index(es, index)
    async with session_factory() as session:
        rows = await orders_repo.fetch_orders_for_sync(session, limit=100_000)
    count = 0
    batch: List[Dict[str, Any]] = []
    for entry in rows:
        batch.append(
            search_repo.build_order_doc(entry["order"], entry["user"], entry["items"])
        )
        if len(batch) >= 100:
            await _upsert_batch(es, index, batch)
            count += len(batch)
            batch = []
    if batch:
        await _upsert_batch(es, index, batch)
        count += len(batch)
    try:
        await es.indices.refresh(index=index)
    except Exception:
        pass
    log.info("reindex complete", extra={"indexed": count})
    return count


async def _upsert_batch(es: Any, index: str, docs: List[Dict[str, Any]]) -> None:
    await asyncio.gather(*[search_repo.upsert_order_doc(es, index, d) for d in docs])
