"""Order tests: unit (fake Mongo/ES + live PG) and integration (real DBs)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.tables import Order, OrderItem, Outbox
from app.repositories import orders_repo
from .conftest import arun, call, make_app, make_itest_app


def _checkout_body(ids, key=None):
    body = {
        "user_id": None,  # filled by caller
        "items": [
            {"product_id": ids["WM-001"], "quantity": 2},
            {"product_id": ids["MK-101"], "quantity": 1},
        ],
    }
    if key:
        body["idempotency_key"] = key
    return body


def _counts(session_factory):
    async def _go():
        async with session_factory() as s:
            n_orders = (await s.execute(select(func.count(Order.id)))).scalar()
            n_items = (await s.execute(select(func.count(OrderItem.id)))).scalar()
            n_outbox = (await s.execute(select(func.count(Outbox.id)))).scalar()
        return n_orders, n_items, n_outbox

    return arun(_go())


# ------------------------------------------------------------ unit (fakes) ---
def test_checkout_happy_path(seeded_mongo, fake_es, pg_user, session_factory):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    body = _checkout_body(ids, key="key-happy-1")
    body["user_id"] = pg_user
    status, data = call(app, "POST", "/api/orders", json=body)
    assert status == 201, data
    # server-side prices win: 2*50.16 + 1*100.34 = 200.66
    assert Decimal(str(data["total_amount"])) == Decimal("200.66")
    assert data["status"] == "PENDING"
    assert data["es_sync"] == {"strategy": "dual_write", "status": "synced"}
    titles = {i["title"]: i for i in data["items"]}
    assert Decimal(titles["Wireless Mouse"]["line_total"]) == Decimal("100.32")
    assert _counts(session_factory) == (1, 2, 0)
    # ES doc with deterministic id + snapshots
    doc = fake_es._docs["orders"][f"order-{data['order_id']}"]
    assert doc["customer"]["name"] == "Test User"
    assert doc["total_amount"] == pytest.approx(200.66)
    assert {i["title"] for i in doc["items"]} == {"Wireless Mouse", "Mechanical Keyboard"}


def test_checkout_validation(seeded_mongo, fake_es, pg_user):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    # inactive product
    b = {"user_id": pg_user, "items": [{"product_id": ids["CRT-1"], "quantity": 1}]}
    assert call(app, "POST", "/api/orders", json=b)[0] == 422
    # missing product
    b["items"] = [{"product_id": "000000000000000000000000", "quantity": 1}]
    assert call(app, "POST", "/api/orders", json=b)[0] == 422
    # bad quantity
    b["items"] = [{"product_id": ids["WM-001"], "quantity": 0}]
    assert call(app, "POST", "/api/orders", json=b)[0] == 422
    # unknown user
    b = {"user_id": 999999, "items": [{"product_id": ids["WM-001"], "quantity": 1}]}
    status, data = call(app, "POST", "/api/orders", json=b)
    assert status == 400 and "detail" in data
    # empty items
    b = {"user_id": pg_user, "items": []}
    assert call(app, "POST", "/api/orders", json=b)[0] == 422


def test_checkout_atomic_rollback(seeded_mongo, fake_es, pg_user, session_factory,
                                 monkeypatch):
    """If any item insert fails, the whole order is rolled back."""
    db, ids = seeded_mongo
    calls = {"n": 0}
    RealItem = orders_repo.OrderItem

    def exploding_item(*a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("simulated item insert failure")
        return RealItem(*a, **k)

    monkeypatch.setattr(orders_repo, "OrderItem", exploding_item)
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    body = _checkout_body(ids, key="key-rollback-1")
    body["user_id"] = pg_user
    with pytest.raises(RuntimeError, match="simulated item insert failure"):
        call(app, "POST", "/api/orders", json=body)
    assert _counts(session_factory) == (0, 0, 0), "partial order leaked despite rollback"


def test_checkout_idempotent_replay(seeded_mongo, fake_es, pg_user, session_factory):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    body = _checkout_body(ids, key="key-idem-1")
    body["user_id"] = pg_user
    s1, d1 = call(app, "POST", "/api/orders", json=body)
    s2, d2 = call(app, "POST", "/api/orders", json=body)
    assert s1 == 201 and s2 == 200
    assert d1["order_id"] == d2["order_id"]
    assert _counts(session_factory) == (1, 2, 0)


def test_get_order_detail_from_pg(seeded_mongo, fake_es, pg_user):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    body = _checkout_body(ids)
    body["user_id"] = pg_user
    _, created = call(app, "POST", "/api/orders", json=body)
    status, d = call(app, "GET", f"/api/orders/{created['order_id']}")
    assert status == 200
    assert d["customer"]["email"] == "test.user@example.com"
    assert d["sync"]["es_in_sync"] is True
    assert d["sync"]["es_updated_at"] is not None
    status, data = call(app, "GET", "/api/orders/999999")
    assert status == 404 and "detail" in data


def test_status_update_syncs_es(seeded_mongo, fake_es, pg_user):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    body = _checkout_body(ids)
    body["user_id"] = pg_user
    _, created = call(app, "POST", "/api/orders", json=body)
    oid = created["order_id"]
    status, data = call(app, "PATCH", f"/api/orders/{oid}/status",
                        json={"status": "SHIPPED"})
    assert status == 200
    assert data["es_sync"]["status"] == "synced"
    _, d = call(app, "GET", f"/api/orders/{oid}")
    assert d["status"] == "SHIPPED"
    assert call(app, "PATCH", f"/api/orders/{oid}/status",
                json={"status": "BOGUS"})[0] == 422
    assert call(app, "PATCH", "/api/orders/999999/status",
                json={"status": "SHIPPED"})[0] == 404
    assert fake_es._docs["orders"][f"order-{oid}"]["status"] == "SHIPPED"


def test_snapshot_preserved_after_catalog_edit(seeded_mongo, fake_es, pg_user):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    body = {"user_id": pg_user, "items": [{"product_id": ids["WM-001"], "quantity": 1}]}
    _, created = call(app, "POST", "/api/orders", json=body)
    # catalog edit: rename + reprice (Mongo only)
    call(app, "PUT", f"/api/products/{ids['WM-001']}",
         json={"title": "Wireless Mouse Pro", "price": 59.99})
    _, detail = call(app, "GET", f"/api/orders/{created['order_id']}")
    assert detail["items"][0]["title"] == "Wireless Mouse"
    assert Decimal(str(detail["items"][0]["unit_price"])) == Decimal("50.16")
    # ...but a NEW checkout uses the current catalog price/title
    _, again = call(app, "POST", "/api/orders", json=body)
    assert again["items"][0]["title"] == "Wireless Mouse Pro"
    assert Decimal(str(again["total_amount"])) == Decimal("59.99")


def test_es_down_checkout_still_commits_pg(seeded_mongo, pg_user, session_factory):
    """ES unreachable: PG commit succeeds, outbox row queued, never faked rollback."""
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db, fake_es=None)  # None = ES unreachable
    body = _checkout_body(ids, key="key-esdown-1")
    body["user_id"] = pg_user
    status, data = call(app, "POST", "/api/orders", json=body)
    assert status == 201
    assert data["es_sync"] == {"strategy": "dual_write", "status": "pending_retry"}
    assert _counts(session_factory) == (1, 2, 1)


# ------------------------------------------------- integration (real DBs) ---
def test_integration_checkout_search_and_status(itest_settings, real_mongo_db,
                                                real_es_client, itest_mongo_products,
                                                pg_user, session_factory):
    from app.models import schemas as _schemas
    from app.repositories import search_repo

    app, _ = make_itest_app(real_mongo_db, real_es_client, itest_settings)
    ids = itest_mongo_products
    body = {"user_id": pg_user,
            "items": [{"product_id": ids["T-WM-001"], "quantity": 2}],
            "idempotency_key": "itest-key-1"}
    status, data = call(app, "POST", "/api/orders", json=body)
    assert status == 201, data
    assert Decimal(str(data["total_amount"])) == Decimal("100.32")
    assert data["es_sync"]["status"] == "synced"
    oid = data["order_id"]

    _, detail = call(app, "GET", f"/api/orders/{oid}")
    assert detail["sync"]["es_in_sync"] is True
    assert detail["items"][0]["title"] == "Wireless Mouse"

    status, _ = call(app, "PATCH", f"/api/orders/{oid}/status",
                     json={"status": "PROCESSING"})
    assert status == 200

    async def _check():
        await real_es_client.indices.refresh(index="orders_test")
        doc = await search_repo.get_order_doc(real_es_client, "orders_test", oid)
        assert doc["status"] == "PROCESSING"
        params = _schemas.SearchRequest(q="wireless", page_size=10)
        resp = await search_repo.search_orders(real_es_client, "orders_test", params)
        assert resp.total >= 1
        assert resp.aggs.total_orders >= 1
        assert resp.aggs.revenue > 0

    arun(_check())


def test_integration_outbox_strategy(itest_settings, real_mongo_db, real_es_client,
                                     itest_mongo_products, pg_user, session_factory):
    """Outbox strategy: row written atomically in the PG txn, worker syncs ES."""
    from app.repositories import search_repo
    from app.services import sync_service

    settings = itest_settings
    settings.SYNC_STRATEGY = "outbox"
    app, _ = make_itest_app(real_mongo_db, real_es_client, settings)
    ids = itest_mongo_products
    body = {"user_id": pg_user,
            "items": [{"product_id": ids["T-MK-101"], "quantity": 1}],
            "idempotency_key": "itest-key-outbox-1"}
    status, data = call(app, "POST", "/api/orders", json=body)
    assert status == 201
    assert data["es_sync"] == {"strategy": "outbox", "status": "queued"}
    oid = data["order_id"]

    async def _check():
        # outbox row exists BEFORE the worker runs (atomic with the order txn)
        async with session_factory() as s:
            pending = (await s.execute(
                select(func.count(Outbox.id)).where(Outbox.status == "pending")
            )).scalar()
        assert pending == 1
        # ES has nothing yet
        assert await search_repo.get_order_doc(real_es_client, "orders_test", oid) is None
        # worker drains it
        result = await sync_service.process_outbox_once(
            session_factory, real_es_client, "orders_test", settings)
        assert result == {"processed": 1, "failed": 0}
        await real_es_client.indices.refresh(index="orders_test")
        doc = await search_repo.get_order_doc(real_es_client, "orders_test", oid)
        assert doc["total_amount"] == pytest.approx(100.34)
        # idempotent replay: processing an empty outbox is a no-op
        again = await sync_service.process_outbox_once(
            session_factory, real_es_client, "orders_test", settings)
        assert again == {"processed": 0, "failed": 0}

    arun(_check())
