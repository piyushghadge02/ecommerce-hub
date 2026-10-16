"""Search tests: unit (query-body construction, fake ES) + integration (real ES)."""
from __future__ import annotations

from datetime import date

import pytest

from app.models import schemas
from app.repositories import search_repo
from .conftest import arun, call, make_app, make_itest_app


def _params(**kw):
    base = dict(q=None, statuses=None, page=1, page_size=20)
    base.update(kw)
    return schemas.SearchRequest(**base)


# ------------------------------------------------------------ unit (fake) ---
def test_build_query_text_search():
    body = search_repo.build_search_body(_params(q="wireless"))
    should = body["query"]["bool"]["must"][0]["bool"]["should"]
    kinds = set()
    for clause in should:
        if "multi_match" in clause:
            kinds.add("multi_match")
            mm = clause["multi_match"]
            assert mm["query"] == "wireless"
            assert set(mm["fields"]) == {"customer.name", "customer.email", "items.title"}
        if "nested" in clause:
            kinds.add("nested")
            assert clause["nested"]["path"] == "items"
    assert kinds == {"multi_match", "nested"}


def test_build_query_filters_sort_pagination():
    body = search_repo.build_search_body(_params(
        statuses=["PENDING", "SHIPPED"],
        date_from=date(2026, 7, 1), date_to=date(2026, 10, 16),
        min_total=30, max_total=150,
        sort_by="total_amount", sort_dir="asc", page=3, page_size=10,
    ))
    filters = body["query"]["bool"]["filter"]
    terms = next(f["terms"] for f in filters if "terms" in f)
    ranges = [f["range"] for f in filters if "range" in f]
    assert terms == {"status": ["PENDING", "SHIPPED"]}
    assert {"order_date": {"gte": "2026-07-01", "lte": "2026-10-16T23:59:59"}} in ranges
    assert {"total_amount": {"gte": 30.0, "lte": 150.0}} in ranges
    assert body["sort"] == [{"total_amount": {"order": "asc"}}]
    assert body["from"] == 20 and body["size"] == 10
    # aggregations ride on the same filtered query
    assert set(body["aggs"]) == {"revenue", "by_status", "total_orders"}
    assert body["aggs"]["revenue"] == {"sum": {"field": "total_amount"}}


def test_build_query_match_all_when_no_filters():
    body = search_repo.build_search_body(_params())
    assert body["query"] == {"match_all": {}}


def test_parse_search_response():
    canned = {
        "hits": {
            "total": {"value": 2, "relation": "eq"},
            "hits": [
                {"_source": {
                    "order_id": 7, "order_date": "2026-10-01T10:00:00+00:00",
                    "status": "SHIPPED", "total_amount": 100.5,
                    "updated_at": "2026-10-02T10:00:00+00:00",
                    "customer": {"id": 3, "name": "Wendy Wireless",
                                 "email": "wendy.wireless@example.com"},
                    "items": []}},
                {"_source": {
                    "order_id": 9, "order_date": "2026-10-03T10:00:00+00:00",
                    "status": "PENDING", "total_amount": 25.0,
                    "updated_at": "2026-10-03T10:00:00+00:00",
                    "customer": {"id": 1, "name": "John Doe",
                                 "email": "john.doe@example.com"},
                    "items": []}},
            ],
        },
        "aggregations": {
            "revenue": {"value": 125.5},
            "by_status": {"buckets": [
                {"key": "SHIPPED", "doc_count": 1},
                {"key": "PENDING", "doc_count": 1}]},
            "total_orders": {"value": 2},
        },
    }
    resp = search_repo.parse_search_response(canned, _params(page_size=20))
    assert resp.total == 2 and resp.total_pages == 1
    assert resp.orders[0].customer.name == "Wendy Wireless"
    assert resp.aggs.revenue == pytest.approx(125.5)
    assert resp.aggs.by_status == {"SHIPPED": 1, "PENDING": 1}
    assert resp.aggs.total_orders == 2


def test_search_api_uses_es_only(fake_es):
    app, _ = make_app(fake_es=fake_es)
    status, data = call(app, "POST", "/api/search/orders", json={
        "q": "wireless", "statuses": ["PENDING"],
        "min_total": 10, "sort_by": "order_date", "sort_dir": "desc",
        "page": 1, "page_size": 20,
    })
    assert status == 200
    body = fake_es.last_search_body
    assert body is not None
    assert body["query"]["bool"]["filter"][0] == {"terms": {"status": ["PENDING"]}}
    assert data["total"] == 0 and data["aggs"]["revenue"] == 0.0


def test_search_api_es_unreachable_returns_503():
    app, _ = make_app(fake_es=None)  # None = ES unreachable
    status, data = call(app, "POST", "/api/search/orders", json={"q": "x"})
    assert status == 503
    assert "detail" in data


# ------------------------------------------------- integration (real ES) ---
def _seed_docs(real_es_client):
    async def _go():
        await search_repo.ensure_index(real_es_client, "orders_test")
        docs = [
            {"order_id": 1, "order_date": "2026-09-01T10:00:00+00:00", "status": "PENDING",
             "total_amount": 25.0, "updated_at": "2026-09-01T10:00:00+00:00",
             "customer": {"id": 3, "name": "Wendy Wireless", "email": "wendy.wireless@example.com"},
             "items": [{"product_id": "a", "title": "Wireless Mouse", "quantity": 1, "unit_price": 25.0}]},
            {"order_id": 2, "order_date": "2026-08-01T10:00:00+00:00", "status": "SHIPPED",
             "total_amount": 250.0, "updated_at": "2026-08-02T10:00:00+00:00",
             "customer": {"id": 1, "name": "John Doe", "email": "john.doe@example.com"},
             "items": [{"product_id": "b", "title": "Mechanical Keyboard", "quantity": 1, "unit_price": 250.0}]},
            {"order_id": 3, "order_date": "2026-09-15T10:00:00+00:00", "status": "PROCESSING",
             "total_amount": 100.0, "updated_at": "2026-09-15T10:00:00+00:00",
             "customer": {"id": 3, "name": "Wendy Wireless", "email": "wendy.wireless@example.com"},
             "items": [{"product_id": "c", "title": "USB Cable", "quantity": 4, "unit_price": 25.0}]},
        ]
        for d in docs:
            await search_repo.upsert_order_doc(real_es_client, "orders_test", d)
        await real_es_client.indices.refresh(index="orders_test")

    arun(_go())


def test_integration_search_text_and_filters(real_es_client):
    _seed_docs(real_es_client)

    async def _go():
        # partial term matches product title AND customer name
        r = await search_repo.search_orders(real_es_client, "orders_test", _params(q="wireless"))
        assert r.total == 2  # order 1 (item title) + order 3 (customer name)
        assert {o.order_id for o in r.orders} == {1, 3}
        # status filter narrows
        r = await search_repo.search_orders(
            real_es_client, "orders_test", _params(q="wireless", statuses=["PENDING"]))
        assert r.total == 1 and r.orders[0].order_id == 1
        # price filter
        r = await search_repo.search_orders(
            real_es_client, "orders_test", _params(min_total=200))
        assert r.total == 1 and r.orders[0].order_id == 2
        # date filter
        r = await search_repo.search_orders(
            real_es_client, "orders_test",
            _params(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)))
        assert r.total == 2
        # aggregations reflect the ACTIVE filters (not the whole index)
        r = await search_repo.search_orders(
            real_es_client, "orders_test", _params(statuses=["PENDING", "PROCESSING"]))
        assert r.aggs.total_orders == 2
        assert r.aggs.revenue == pytest.approx(125.0)
        assert r.aggs.by_status == {"PENDING": 1, "PROCESSING": 1}
        # sorting + pagination
        r = await search_repo.search_orders(
            real_es_client, "orders_test",
            _params(sort_by="total_amount", sort_dir="desc", page=1, page_size=2))
        assert [o.order_id for o in r.orders] == [2, 3]
        assert r.total_pages == 2
        r = await search_repo.search_orders(
            real_es_client, "orders_test",
            _params(sort_by="total_amount", sort_dir="desc", page=2, page_size=2))
        assert [o.order_id for o in r.orders] == [1]

    arun(_go())


def test_integration_search_api_end_to_end(real_es_client, itest_settings,
                                           real_mongo_db):
    _seed_docs(real_es_client)
    app, _ = make_itest_app(real_mongo_db, real_es_client, itest_settings)
    status, data = call(app, "POST", "/api/search/orders",
                        json={"q": "wendy", "page_size": 5})
    assert status == 200
    assert data["total"] == 2
    assert data["aggs"]["by_status"]["PENDING"] == 1
    assert data["orders"][0]["customer"]["name"] == "Wendy Wireless"
