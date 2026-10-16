"""Elasticsearch repository: the `orders` index.

The admin search dashboard reads ONLY from Elasticsearch. Documents are
denormalized snapshots (titles/prices copied at checkout) with deterministic
document IDs `order-<order_id>` so every upsert is idempotent.

`items` uses a **nested** mapping: a plain object mapping would flatten the
items arrays and produce false cross-item matches (e.g. title from item A
matching product_id from item B). The search query therefore uses a nested
query for `items.title` alongside a top-level multi_match.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import elasticsearch

from ..core.logging import get_logger
from ..models import schemas

log = get_logger(__name__)

ORDER_DOC_ID = "order-{order_id}"

ORDERS_MAPPING: Dict[str, Any] = {
    "mappings": {
        "properties": {
            "order_id": {"type": "long"},
            "order_date": {"type": "date"},
            "status": {"type": "keyword"},
            "total_amount": {"type": "double"},
            "updated_at": {"type": "date"},
            "customer": {
                "type": "object",
                "properties": {
                    "id": {"type": "long"},
                    "name": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    },
                    "email": {"type": "keyword"},
                },
            },
            "items": {
                "type": "nested",
                "properties": {
                    "product_id": {"type": "keyword"},
                    "title": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    },
                    "quantity": {"type": "integer"},
                    "unit_price": {"type": "double"},
                },
            },
        }
    }
}


def _body(resp: Any) -> Any:
    """Normalize elasticsearch-py responses and plain dicts (fakes)."""
    return resp.body if hasattr(resp, "body") else resp


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, elasticsearch.NotFoundError) or getattr(
        exc, "status_code", None
    ) == 404


def doc_id(order_id: int) -> str:
    return ORDER_DOC_ID.format(order_id=order_id)


def build_order_doc(
    order: Any, user: Any, items: Sequence[Any]
) -> Dict[str, Any]:
    """Build the denormalized ES document from PG rows (snapshots only)."""
    def _iso(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    return {
        "order_id": order.id,
        "order_date": _iso(order.order_date),
        "status": order.status,
        "total_amount": float(order.total_amount),
        "updated_at": _iso(order.updated_at),
        "customer": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
        },
        "items": [
            {
                "product_id": item.product_id,
                "title": item.title,
                "quantity": item.quantity,
                "unit_price": float(item.unit_price),
            }
            for item in items
        ],
    }


# ------------------------------------------------------------ index ops ---
async def ensure_index(es: Any, index: str) -> bool:
    """Create the index with the explicit mapping if missing. Never raises."""
    try:
        resp = await es.indices.exists(index=index)
        # elasticsearch-py 8.x returns HeadApiResponse (whose .body is a bool);
        # fakes may return a plain bool or {"exists": ...}.
        body = resp.body if hasattr(resp, "body") else resp
        exists = bool(body["exists"]) if isinstance(body, dict) else bool(body)
        if not exists:
            await es.indices.create(index=index, **ORDERS_MAPPING)
            log.info("elasticsearch index created", extra={"index": index})
        return True
    except Exception as exc:
        log.warning("elasticsearch ensure_index failed", extra={"error": str(exc)})
        return False


async def upsert_order_doc(es: Any, index: str, doc: Dict[str, Any]) -> None:
    await es.index(index=index, id=doc_id(doc["order_id"]), document=doc)


async def get_order_doc(es: Any, index: str, order_id: int) -> Optional[Dict[str, Any]]:
    try:
        resp = await es.get(index=index, id=doc_id(order_id))
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise
    return _body(resp).get("_source")


async def count_docs(es: Any, index: str) -> Optional[int]:
    try:
        resp = await es.count(index=index)
        return int(_body(resp)["count"])
    except Exception as exc:
        log.warning("elasticsearch count failed", extra={"error": str(exc)})
        return None


async def delete_index(es: Any, index: str) -> None:
    try:
        await es.indices.delete(index=index, ignore_unavailable=True)
    except Exception as exc:
        log.warning("elasticsearch delete_index failed", extra={"error": str(exc)})


# --------------------------------------------------------------- search ---
SORT_FIELDS = {
    "order_date": "order_date",
    "total_amount": "total_amount",
    "order_id": "order_id",
}


def build_search_body(params: schemas.SearchRequest) -> Dict[str, Any]:
    """Build the ES query body: bool + multi_match (+ nested) + filters + aggs.

    Aggregations run in the SAME filter context as the hits, so revenue and
    status counts always describe exactly the currently filtered result set.
    """
    must: List[Dict[str, Any]] = []
    if params.q:
        must.append(
            {
                "bool": {
                    "should": [
                        {
                            "multi_match": {
                                "query": params.q,
                                "fields": ["customer.name", "customer.email", "items.title"],
                                "type": "best_fields",
                            }
                        },
                        {
                            "nested": {
                                "path": "items",
                                "query": {"match": {"items.title": params.q}},
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            }
        )

    filters: List[Dict[str, Any]] = []
    if params.statuses:
        filters.append({"terms": {"status": list(params.statuses)}})
    if params.date_from or params.date_to:
        rng: Dict[str, Any] = {}
        if params.date_from:
            rng["gte"] = params.date_from.isoformat()
        if params.date_to:
            # inclusive end of day
            rng["lte"] = params.date_to.isoformat() + "T23:59:59"
        filters.append({"range": {"order_date": rng}})
    if params.min_total is not None or params.max_total is not None:
        rng = {}
        if params.min_total is not None:
            rng["gte"] = params.min_total
        if params.max_total is not None:
            rng["lte"] = params.max_total
        filters.append({"range": {"total_amount": rng}})

    query: Dict[str, Any] = {"bool": {}}
    if must:
        query["bool"]["must"] = must
    if filters:
        query["bool"]["filter"] = filters
    if not must and not filters:
        query = {"match_all": {}}

    from_ = (params.page - 1) * params.page_size
    return {
        "query": query,
        "sort": [{SORT_FIELDS[params.sort_by]: {"order": params.sort_dir}}],
        "from": from_,
        "size": params.page_size,
        "aggs": {
            "revenue": {"sum": {"field": "total_amount"}},
            "by_status": {"terms": {"field": "status", "size": 10}},
            "total_orders": {"value_count": {"field": "order_id"}},
        },
        "track_total_hits": True,
    }


def _parse_hit(source: Dict[str, Any]) -> schemas.SearchOrderRow:
    customer = source.get("customer", {}) or {}
    return schemas.SearchOrderRow(
        order_id=int(source["order_id"]),
        order_date=_coerce_dt(source["order_date"]),
        customer=schemas.SearchCustomer(
            id=int(customer.get("id", 0)),
            name=str(customer.get("name", "")),
            email=str(customer.get("email", "")),
        ),
        total_amount=float(source.get("total_amount", 0.0)),
        status=source.get("status", "PENDING"),
    )


def _coerce_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_search_response(
    body: Dict[str, Any], params: schemas.SearchRequest
) -> schemas.SearchResponse:
    hits = body.get("hits", {}) or {}
    total_info = hits.get("total", 0)
    total = total_info.get("value", 0) if isinstance(total_info, dict) else int(total_info)
    orders = [_parse_hit(h.get("_source", {})) for h in hits.get("hits", [])]

    aggs = body.get("aggregations", {}) or {}
    revenue = (aggs.get("revenue", {}) or {}).get("value") or 0.0
    by_status = {
        b["key"]: int(b["doc_count"])
        for b in (aggs.get("by_status", {}) or {}).get("buckets", [])
    }
    total_orders = int((aggs.get("total_orders", {}) or {}).get("value", 0))

    total_pages = max(1, -(-total // params.page_size)) if total else 1
    return schemas.SearchResponse(
        total=total,
        page=params.page,
        page_size=params.page_size,
        total_pages=total_pages,
        orders=orders,
        aggs=schemas.SearchAggs(
            revenue=float(revenue),
            by_status=by_status,
            total_orders=total_orders,
        ),
    )


async def search_orders(
    es: Any, index: str, params: schemas.SearchRequest
) -> schemas.SearchResponse:
    body = build_search_body(params)
    try:
        resp = await es.search(index=index, **body)
    except elasticsearch.NotFoundError:
        # Index missing (not seeded yet): behave like an empty result set.
        return schemas.SearchResponse(
            total=0,
            page=params.page,
            page_size=params.page_size,
            total_pages=1,
            orders=[],
            aggs=schemas.SearchAggs(revenue=0.0, by_status={}, total_orders=0),
        )
    return parse_search_response(_body(resp), params)
