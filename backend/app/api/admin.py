"""Admin API: sync backlog visibility and full ES rebuild from PostgreSQL."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..core.config import Settings, get_settings
from ..core.database import get_es_client, get_session_factory
from ..models import schemas
from ..repositories import orders_repo, search_repo
from ..services import sync_service

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/sync-status", response_model=schemas.SyncStatusOut)
async def sync_status(
    es: Any = Depends(get_es_client),
    settings: Settings = Depends(get_settings),
):
    async with get_session_factory()() as session:
        counts = await orders_repo.count_outbox_by_status(session)
        watermark = await orders_repo.get_sync_state(
            session, orders_repo.POLLING_WATERMARK_KEY
        )
    docs = await search_repo.count_docs(es, settings.ES_INDEX) if es else None
    return schemas.SyncStatusOut(
        outbox_pending=int(counts.get("pending", 0)) + int(counts.get("processing", 0)),
        outbox_failed=int(counts.get("failed", 0)),
        polling_watermark=watermark,
        elasticsearch_docs=docs,
    )


@router.post("/reindex", response_model=schemas.ReindexOut)
async def reindex(
    es: Any = Depends(get_es_client),
    settings: Settings = Depends(get_settings),
):
    if es is None:
        raise HTTPException(
            status_code=503, detail="reindex unavailable: elasticsearch unreachable"
        )
    indexed = await sync_service.reindex_all(
        get_session_factory(), es, settings.ES_INDEX
    )
    return schemas.ReindexOut(indexed=indexed)
