"""Sync strategy tests + admin/health endpoints.

Unit coverage uses FakeES (down/up); integration coverage uses the real ES
test index and live PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models import schemas
from app.models.tables import Order, Outbox, User
from app.repositories import orders_repo, search_repo
from app.services import order_service, sync_service
from .conftest import TEST_ES_INDEX, arun, call, make_app, make_itest_app
from .conftest import FakeES


def _settings(strategy: str) -> Settings:
    return Settings(SYNC_STRATEGY=strategy)


def _make_user_id(session_factory, name="Sync User", email="sync.user@example.com"):
    async def _go():
        async with session_factory() as s:
            u = User(name=name, email=email)
            s.add(u)
            await s.commit()
            await s.refresh(u)
            return u.id

    return arun(_go())


def _make_order_id(session_factory, user_id, total="10.00"):
    async def _go():
        async with session_factory() as s:
            order, _ = await orders_repo.create_order(
                s, user_id=user_id,
                lines=[("prod-1", "Test Product", 1, Decimal(total))],
            )
            return order.id

    return arun(_go())


# ------------------------------------------------------------ unit (fakes) ---
def test_dual_write_failure_enqueues_repair(seeded_mongo, pg_user, session_factory):
    """ES down at checkout: PG commits, outbox repair row queued, worker recovers."""
    db, ids = seeded_mongo
    es = FakeES()
    es.down = True
    settings = _settings("dual_write")
    payload = schemas.OrderCreate(
        user_id=pg_user,
        items=[schemas.OrderItemCreate(product_id=ids["WM-001"], quantity=1)],
        idempotency_key="sync-dw-1",
    )

    async def _go():
        out, code = await order_service.place_order(
            session_factory, db, es, settings, payload)
        assert code == 201
        assert out.es_sync.status == "pending_retry"
        async with session_factory() as s:
            n = (await s.execute(
                select(func.count(Outbox.id)).where(Outbox.status == "pending"))).scalar()
            assert n == 1
            n_orders = (await s.execute(select(func.count(Order.id)))).scalar()
            assert n_orders == 1  # PG truth intact
        # ES recovers -> repair loop drains the outbox
        es.down = False
        result = await sync_service.process_outbox_once(
            session_factory, es, "orders", settings)
        assert result == {"processed": 1, "failed": 0}
        doc = es._docs["orders"][f"order-{out.order_id}"]
        assert doc["customer"]["name"] == "Test User"
        return out.order_id

    arun(_go())

    async def _check_done():
        async with session_factory() as s:
            n = (await s.execute(
                select(func.count(Outbox.id)).where(Outbox.status == "done"))).scalar()
            assert n == 1

    arun(_check_done())


def test_outbox_worker_backoff_then_failed(seeded_mongo, pg_user, session_factory):
    db, ids = seeded_mongo
    es = FakeES()
    es.down = True  # ES stays down
    settings = _settings("outbox")
    payload = schemas.OrderCreate(
        user_id=pg_user,
        items=[schemas.OrderItemCreate(product_id=ids["WM-001"], quantity=1)],
    )

    async def _go():
        out, _ = await order_service.place_order(
            session_factory, db, es, settings, payload)
        assert out.es_sync.status == "queued"
        before = datetime.now(timezone.utc)
        result = await sync_service.process_outbox_once(
            session_factory, es, "orders", settings)
        assert result == {"processed": 0, "failed": 1}
        async with session_factory() as s:
            row = (await s.execute(select(Outbox))).scalars().first()
            assert row.status == "pending" and row.attempts == 1
            assert row.next_attempt_at > before  # exponential backoff scheduled
            assert "fake elasticsearch is down" in row.last_error
            # force to the last attempt -> next failure marks it failed
            row.attempts = settings.OUTBOX_MAX_ATTEMPTS - 1
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await s.commit()
        result = await sync_service.process_outbox_once(
            session_factory, es, "orders", settings)
        assert result == {"processed": 0, "failed": 1}
        async with session_factory() as s:
            row = (await s.execute(select(Outbox))).scalars().first()
            assert row.status == "failed"
            assert row.attempts == settings.OUTBOX_MAX_ATTEMPTS

    arun(_go())


def test_upsert_idempotent_deterministic_id():
    es = FakeES()
    doc = {"order_id": 42, "status": "PENDING", "total_amount": 9.99,
           "order_date": "2026-10-01T00:00:00+00:00",
           "updated_at": "2026-10-01T00:00:00+00:00",
           "customer": {"id": 1, "name": "N", "email": "n@e.com"}, "items": []}

    async def _go():
        await sync_service.push_order_doc(es, "orders", doc)
        await sync_service.push_order_doc(es, "orders", doc)  # retry: same id
        assert list(es._docs["orders"]) == ["order-42"]

    arun(_go())


def test_admin_sync_status_and_health(seeded_mongo, fake_es):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    status, h = call(app, "GET", "/api/health")
    assert status == 200
    assert h["status"] == "ok"
    assert h["postgres"] == "up" and h["mongodb"] == "up"
    assert h["elasticsearch"] == "up"
    assert h["sync_strategy"] == "dual_write"
    status, s = call(app, "GET", "/api/admin/sync-status")
    assert s["outbox_pending"] == 0 and s["outbox_failed"] == 0
    assert s["elasticsearch_docs"] == 0


def test_health_degraded_when_es_down():
    app, _ = make_app(fake_es=None)
    status, h = call(app, "GET", "/api/health")
    assert h["status"] == "degraded"
    assert h["elasticsearch"] == "down"


# ------------------------------------------------- integration (real DBs) ---
def test_integration_polling_worker(itest_settings, real_es_client,
                                    pg_user, session_factory):
    """Polling: watermark advances, changed orders get synced on next cycle."""
    settings = itest_settings
    settings.SYNC_STRATEGY = "polling"
    oid = _make_order_id(session_factory, pg_user, total="33.33")

    async def _go():
        r1 = await sync_service.run_polling_cycle(
            session_factory, real_es_client, TEST_ES_INDEX)
        assert r1["synced"] == 1
        await real_es_client.indices.refresh(index=TEST_ES_INDEX)
        doc = await search_repo.get_order_doc(real_es_client, TEST_ES_INDEX, oid)
        assert doc["total_amount"] == pytest.approx(33.33)
        assert doc["status"] == "PENDING"
        # second cycle: nothing new
        r2 = await sync_service.run_polling_cycle(
            session_factory, real_es_client, TEST_ES_INDEX)
        assert r2["synced"] == 0
        # status change bumps updated_at -> next cycle syncs it
        async with session_factory() as s:
            await orders_repo.update_order_status(s, oid, "SHIPPED")
        r3 = await sync_service.run_polling_cycle(
            session_factory, real_es_client, TEST_ES_INDEX)
        assert r3["synced"] == 1
        await real_es_client.indices.refresh(index=TEST_ES_INDEX)
        doc = await search_repo.get_order_doc(real_es_client, TEST_ES_INDEX, oid)
        assert doc["status"] == "SHIPPED"

    arun(_go())


def test_integration_reindex_endpoint(itest_settings, real_mongo_db, real_es_client,
                                      pg_user, session_factory):
    """POST /api/admin/reindex rebuilds the ES index from PostgreSQL only."""
    app, _ = make_itest_app(real_mongo_db, real_es_client, itest_settings)
    oid1 = _make_order_id(session_factory, pg_user, total="10.00")
    oid2 = _make_order_id(session_factory, pg_user, total="20.00")

    async def _bump():
        async with session_factory() as s:
            await orders_repo.update_order_status(s, oid2, "SHIPPED")

    arun(_bump())

    status, data = call(app, "POST", "/api/admin/reindex")
    assert status == 200
    assert data["indexed"] == 2
    status, s = call(app, "GET", "/api/admin/sync-status")
    assert s["elasticsearch_docs"] == 2

    async def _check():
        d1 = await search_repo.get_order_doc(real_es_client, TEST_ES_INDEX, oid1)
        d2 = await search_repo.get_order_doc(real_es_client, TEST_ES_INDEX, oid2)
        assert d1["items"][0]["title"] == "Test Product"  # PG snapshot, not Mongo
        assert d2["status"] == "SHIPPED"

    arun(_check())


def test_integration_seed_plan_rules():
    """The deterministic seed plan satisfies every §9 rule (pure function)."""
    sys_path = __import__("sys").path
    sys_path.insert(0, "scripts")
    from seed import PRODUCTS, build_order_plan, check_plan

    plan = build_order_plan(PRODUCTS)
    assert check_plan(plan, PRODUCTS) == []
