"""PostgreSQL repository: orders, order items, outbox, sync_state.

All write paths use explicit transaction boundaries. `create_order` inserts
the order, its items, and (optionally) an outbox event inside ONE transaction:
any failure rolls everything back.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from ..models.tables import Order, OrderItem, Outbox, SyncState, User

log = get_logger(__name__)

# (product_id, title_snapshot, quantity, unit_price)
OrderLine = Tuple[str, str, int, Decimal]

POLLING_WATERMARK_KEY = "polling_watermark"


async def find_order_by_idempotency_key(
    session: AsyncSession, idempotency_key: str
) -> Optional[Order]:
    result = await session.execute(
        select(Order).where(Order.idempotency_key == idempotency_key)
    )
    return result.scalars().first()


async def create_order(
    session: AsyncSession,
    *,
    user_id: int,
    lines: Sequence[OrderLine],
    idempotency_key: Optional[str] = None,
    outbox_event_factory: Optional[Callable[[int], Dict[str, Any]]] = None,
    order_date: Optional[datetime] = None,
    updated_at: Optional[datetime] = None,
) -> Tuple[Order, bool]:
    """Insert order + items (+ optional outbox row) atomically.

    `outbox_event_factory(order_id)` builds {"event_type", "payload"} once the
    order id is known (after flush, still inside the same transaction).

    Returns (order, created). If `idempotency_key` matches an existing order,
    no insert happens and (existing_order, False) is returned.
    """
    if idempotency_key:
        existing = await find_order_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return existing, False

    now = datetime.now(timezone.utc)
    total = sum((qty * price for _, _, qty, price in lines), Decimal("0"))
    order = Order(
        user_id=user_id,
        order_date=order_date or now,
        status="PENDING",
        total_amount=total,
        updated_at=updated_at or now,
        idempotency_key=idempotency_key,
    )
    session.add(order)
    await session.flush()  # populate order.id

    for product_id, title, quantity, unit_price in lines:
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product_id,
                title=title,
                quantity=quantity,
                unit_price=unit_price,
            )
        )

    if outbox_event_factory is not None:
        event = outbox_event_factory(order.id)
        session.add(
            Outbox(
                order_id=order.id,
                event_type=event["event_type"],
                payload=event["payload"],
            )
        )

    await session.commit()
    await session.refresh(order)
    log.info("order created", extra={"order_id": order.id, "total": str(total)})
    return order, True


async def get_order_full(session: AsyncSession, order_id: int) -> Optional[Dict[str, Any]]:
    """Canonical order with user + items (single round of queries)."""
    order = await session.get(Order, order_id)
    if order is None:
        return None
    user = await session.get(User, order.user_id)
    items_result = await session.execute(
        select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)
    )
    items = list(items_result.scalars().all())
    return {"order": order, "user": user, "items": items}


async def update_order_status(
    session: AsyncSession,
    order_id: int,
    status: str,
    *,
    outbox_event_factory: Optional[Callable[[int, datetime], Dict[str, Any]]] = None,
) -> Optional[Order]:
    """Update status + updated_at, optionally inserting an outbox row atomically."""
    order = await session.get(Order, order_id)
    if order is None:
        return None
    order.status = status
    order.updated_at = datetime.now(timezone.utc)
    if outbox_event_factory is not None:
        event = outbox_event_factory(order.id, order.updated_at)
        session.add(
            Outbox(
                order_id=order.id,
                event_type=event["event_type"],
                payload=event["payload"],
            )
        )
    await session.commit()
    await session.refresh(order)
    log.info("order status updated", extra={"order_id": order.id, "status": status})
    return order


async def fetch_orders_for_sync(
    session: AsyncSession,
    order_ids: Optional[Sequence[int]] = None,
    *,
    updated_after: Optional[datetime] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch orders joined with user + items for ES document building."""
    stmt = (
        select(Order, User)
        .join(User, User.id == Order.user_id)
        .order_by(Order.updated_at.asc(), Order.id.asc())
    )
    if order_ids is not None:
        stmt = stmt.where(Order.id.in_(list(order_ids)))
    if updated_after is not None:
        stmt = stmt.where(Order.updated_at > updated_after)
    stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).all()

    out: List[Dict[str, Any]] = []
    for order, user in rows:
        items_result = await session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
        )
        out.append(
            {"order": order, "user": user, "items": list(items_result.scalars().all())}
        )
    return out


# ------------------------------------------------------------- outbox ---
async def insert_outbox_event(
    session: AsyncSession, order_id: int, event_type: str, payload: Dict[str, Any]
) -> Outbox:
    """Insert an outbox row in its own transaction (dual-write fallback path)."""
    event = Outbox(order_id=order_id, event_type=event_type, payload=payload)
    session.add(event)
    await session.commit()
    return event


async def claim_outbox_batch(session: AsyncSession, limit: int = 50) -> List[Outbox]:
    """Fetch due pending rows with SKIP LOCKED so multiple workers are safe."""
    result = await session.execute(
        select(Outbox)
        .where(Outbox.status == "pending", Outbox.next_attempt_at <= func.now())
        .order_by(Outbox.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.status = "processing"
    await session.commit()
    return rows


async def mark_outbox_done(session: AsyncSession, outbox_id: int) -> None:
    await session.execute(
        update(Outbox).where(Outbox.id == outbox_id).values(status="done", last_error=None)
    )
    await session.commit()


async def mark_outbox_error(
    session: AsyncSession,
    outbox_id: int,
    error: str,
    *,
    max_attempts: int,
) -> None:
    """Exponential backoff (2^attempts seconds); 'failed' after max_attempts."""
    row = await session.get(Outbox, outbox_id)
    if row is None:
        return
    row.attempts += 1
    row.last_error = error[:2000]
    if row.attempts >= max_attempts:
        row.status = "failed"
    else:
        row.status = "pending"
        delay = 2 ** row.attempts
        row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    await session.commit()


async def count_outbox_by_status(session: AsyncSession) -> Dict[str, int]:
    result = await session.execute(
        select(Outbox.status, func.count(Outbox.id)).group_by(Outbox.status)
    )
    return {status: count for status, count in result.all()}


# --------------------------------------------------------- sync_state ---
async def get_sync_state(session: AsyncSession, key: str) -> Optional[str]:
    row = await session.get(SyncState, key)
    return row.value if row else None


async def set_sync_state(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(SyncState, key)
    if row is None:
        session.add(SyncState(key=key, value=value))
    else:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    await session.commit()


async def get_polling_watermark(session: AsyncSession) -> Optional[datetime]:
    raw = await get_sync_state(session, POLLING_WATERMARK_KEY)
    if not raw:
        return None
    return datetime.fromisoformat(raw)


async def set_polling_watermark(session: AsyncSession, value: datetime) -> None:
    await set_sync_state(session, POLLING_WATERMARK_KEY, value.isoformat())


# -------------------------------------------------------------- admin ---
async def delete_all_demo_data(session: AsyncSession) -> None:
    """Truncate all transactional tables (used by --reset seed). FK order matters."""
    await session.execute(
        text("TRUNCATE outbox, order_items, orders, sync_state, users RESTART IDENTITY CASCADE")
    )
    await session.commit()
