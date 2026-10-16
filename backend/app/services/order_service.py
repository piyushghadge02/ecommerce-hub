"""Checkout and order-status business logic.

Write-path discipline:
- Checkout validates products against the CURRENT MongoDB catalog (server-side
  prices/titles win; client prices are ignored) and persists the order + items
  inside ONE PostgreSQL transaction. Snapshots (title, unit_price) are copied
  into order_items at checkout time.
- Elasticsearch is synchronized only AFTER the PG transaction commits. If ES
  is unavailable the PG order still succeeds; recovery is handled per strategy
  (see sync_service).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..core.logging import get_logger
from ..models import schemas
from ..repositories import orders_repo, products_repo, search_repo, users_repo
from . import sync_service

log = get_logger(__name__)

STATUSES = ("PENDING", "PROCESSING", "SHIPPED")
_MONEY = Decimal("0.01")


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _truncate_to_second(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def build_es_doc(
    order_id: int,
    user: Any,
    lines: List[Tuple[str, str, int, Decimal]],
    *,
    status: str,
    order_date: datetime,
    updated_at: datetime,
) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "order_date": _truncate_to_second(order_date).isoformat(),
        "status": status,
        "total_amount": float(sum((qty * price for _, _, qty, price in lines), Decimal("0"))),
        "updated_at": _truncate_to_second(updated_at).isoformat(),
        "customer": {"id": user.id, "name": user.name, "email": user.email},
        "items": [
            {
                "product_id": pid,
                "title": title,
                "quantity": qty,
                "unit_price": float(price),
            }
            for pid, title, qty, price in lines
        ],
    }


async def _validate_lines(
    mongo_db: Any, items: List[schemas.OrderItemCreate]
) -> List[Tuple[str, str, int, Decimal]]:
    """Validate each line against the live Mongo catalog. Raises HTTPException."""
    lines: List[Tuple[str, str, int, Decimal]] = []
    for item in items:
        product = await products_repo.get_product(mongo_db, item.product_id)
        if product is None:
            raise HTTPException(status_code=422, detail=f"product not found: {item.product_id}")
        if not product.get("active", True):
            raise HTTPException(status_code=422, detail=f"product is inactive: {item.product_id}")
        lines.append(
            (item.product_id, product["title"], item.quantity, _money(product["price"]))
        )
    return lines


def _order_out(
    order: Any,
    user: Any,
    items: List[Any],
    es_sync: schemas.ESSyncInfo,
) -> schemas.OrderCreateOut:
    return schemas.OrderCreateOut(
        order_id=order.id,
        user_id=order.user_id,
        status=order.status,
        total_amount=order.total_amount,
        order_date=order.order_date,
        updated_at=order.updated_at,
        items=[
            schemas.OrderItemOut(
                product_id=i.product_id,
                title=i.title,
                quantity=i.quantity,
                unit_price=i.unit_price,
                line_total=(i.unit_price * i.quantity).quantize(_MONEY, rounding=ROUND_HALF_UP),
            )
            for i in items
        ],
        es_sync=es_sync,
    )


async def _sync_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    es: Any,
    settings: Settings,
    *,
    order_id: int,
    event_type: str,
    doc: Dict[str, Any],
) -> schemas.ESSyncInfo:
    """Synchronize a committed order to ES per SYNC_STRATEGY.

    Returns the es_sync status for the API response. Never raises: ES failures
    are captured as outbox rows / pending_retry, never as fake rollbacks.
    """
    strategy = settings.SYNC_STRATEGY
    if strategy in ("outbox", "polling"):
        # outbox: row already inserted atomically inside the PG transaction.
        # polling: worker picks the order up via the updated_at watermark.
        return schemas.ESSyncInfo(strategy=strategy, status="queued")

    # dual_write: index immediately; on failure enqueue a repair outbox row
    # in a SEPARATE transaction (the order itself stays committed).
    try:
        await sync_service.push_order_doc(es, settings.ES_INDEX, doc)
        return schemas.ESSyncInfo(strategy=strategy, status="synced")
    except Exception as exc:
        log.warning(
            "dual-write to ES failed; enqueueing outbox repair",
            extra={"order_id": order_id, "error": str(exc)},
        )
        async with session_factory() as session:
            await orders_repo.insert_outbox_event(session, order_id, event_type, doc)
        return schemas.ESSyncInfo(strategy=strategy, status="pending_retry")


async def place_order(
    session_factory: async_sessionmaker[AsyncSession],
    mongo_db: Any,
    es: Any,
    settings: Settings,
    payload: schemas.OrderCreate,
) -> Tuple[schemas.OrderCreateOut, int]:
    async with session_factory() as session:
        user = await users_repo.get_user(session, payload.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail=f"unknown user_id: {payload.user_id}")

    lines = await _validate_lines(mongo_db, payload.items)
    now = datetime.now(timezone.utc)

    # Idempotency replay: return the existing order without inserting.
    if payload.idempotency_key:
        async with session_factory() as session:
            existing = await orders_repo.find_order_by_idempotency_key(
                session, payload.idempotency_key
            )
            if existing is not None:
                full = await orders_repo.get_order_full(session, existing.id)
                try:
                    doc = await search_repo.get_order_doc(es, settings.ES_INDEX, existing.id)
                except Exception:
                    doc = None
                log.info("idempotent replay", extra={"order_id": existing.id})
                return (
                    _order_out(
                        full["order"],
                        full["user"],
                        full["items"],
                        schemas.ESSyncInfo(
                            strategy=settings.SYNC_STRATEGY,
                            status="synced" if doc else "queued",
                        ),
                    ),
                    200,
                )

    def make_doc(order_id: int) -> Dict[str, Any]:
        return build_es_doc(
            order_id, user, lines,
            status="PENDING", order_date=now, updated_at=now,
        )

    async with session_factory() as session:
        order, created = await orders_repo.create_order(
            session,
            user_id=payload.user_id,
            lines=lines,
            idempotency_key=payload.idempotency_key,
            order_date=now,
            updated_at=now,
            # Atomic outbox row for the outbox strategy (same PG transaction).
            outbox_event_factory=(
                (lambda oid: {"event_type": "order.created", "payload": make_doc(oid)})
                if settings.SYNC_STRATEGY == "outbox"
                else None
            ),
        )
        assert created

    doc = make_doc(order.id)
    es_sync = await _sync_after_commit(
        session_factory, es, settings,
        order_id=order.id, event_type="order.created", doc=doc,
    )

    async with session_factory() as session:
        full = await orders_repo.get_order_full(session, order.id)
    return (_order_out(full["order"], full["user"], full["items"], es_sync), 201)


async def get_order_detail(
    session_factory: async_sessionmaker[AsyncSession],
    es: Any,
    settings: Settings,
    order_id: int,
) -> schemas.OrderDetailOut:
    async with session_factory() as session:
        full = await orders_repo.get_order_full(session, order_id)
    if full is None:
        raise HTTPException(status_code=404, detail=f"order not found: {order_id}")

    order, user, items = full["order"], full["user"], full["items"]
    pg_updated = _truncate_to_second(order.updated_at)

    es_doc: Optional[Dict[str, Any]] = None
    es_reachable = True
    try:
        es_doc = await search_repo.get_order_doc(es, settings.ES_INDEX, order_id)
    except Exception as exc:
        log.warning("es unreachable for sync check", extra={"error": str(exc)})
        es_reachable = False

    es_updated_at = None
    if not es_reachable:
        es_in_sync: Optional[bool] = None
    elif es_doc is None:
        es_in_sync = False
    else:
        es_updated_at = search_repo._coerce_dt(es_doc.get("updated_at"))
        es_in_sync = (
            _truncate_to_second(es_updated_at) == pg_updated
            and es_doc.get("status") == order.status
            and float(es_doc.get("total_amount", 0.0)) == float(order.total_amount)
        )

    return schemas.OrderDetailOut(
        order_id=order.id,
        user_id=order.user_id,
        customer=schemas.UserOut(id=user.id, name=user.name, email=user.email),
        status=order.status,
        total_amount=order.total_amount,
        order_date=order.order_date,
        updated_at=order.updated_at,
        items=[
            schemas.OrderItemOut(
                product_id=i.product_id,
                title=i.title,
                quantity=i.quantity,
                unit_price=i.unit_price,
                line_total=(i.unit_price * i.quantity).quantize(_MONEY, rounding=ROUND_HALF_UP),
            )
            for i in items
        ],
        sync=schemas.OrderSyncInfo(
            es_in_sync=es_in_sync,
            pg_updated_at=order.updated_at,
            es_updated_at=es_updated_at,
        ),
    )


async def update_order_status(
    session_factory: async_sessionmaker[AsyncSession],
    es: Any,
    settings: Settings,
    order_id: int,
    status: str,
) -> schemas.StatusUpdateOut:
    if status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {status}")

    async with session_factory() as session:
        full = await orders_repo.get_order_full(session, order_id)
        if full is None:
            raise HTTPException(status_code=404, detail=f"order not found: {order_id}")
        user, items = full["user"], full["items"]
        order_date = full["order"].order_date
        lines = [(i.product_id, i.title, i.quantity, _money(i.unit_price)) for i in items]

        def factory(oid: int, updated_at: datetime) -> Dict[str, Any]:
            return {
                "event_type": "order.status_updated",
                "payload": build_es_doc(
                    oid, user, lines,
                    status=status, order_date=order_date, updated_at=updated_at,
                ),
            }

        order = await orders_repo.update_order_status(
            session,
            order_id,
            status,
            outbox_event_factory=(
                factory if settings.SYNC_STRATEGY == "outbox" else None
            ),
        )

    doc = factory(order.id, order.updated_at)["payload"]
    es_sync = await _sync_after_commit(
        session_factory, es, settings,
        order_id=order.id, event_type="order.status_updated", doc=doc,
    )
    return schemas.StatusUpdateOut(
        order_id=order.id, status=order.status, updated_at=order.updated_at, es_sync=es_sync
    )
