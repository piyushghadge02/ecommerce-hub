"""MongoDB repository: product catalog.

The storefront reads products ONLY from MongoDB. Catalog admin writes go to
MongoDB only — historical PostgreSQL order items and Elasticsearch documents
are never rewritten on catalog edits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from ..core.logging import get_logger

log = get_logger(__name__)

PRODUCTS_COLLECTION = "products"


def serialize_product(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw Mongo document into the API shape (ObjectId -> str id)."""
    out = dict(doc)
    out["id"] = str(out.pop("_id"))
    return out


def _active_filter(active: Optional[bool]) -> Dict[str, Any]:
    if active is None:
        return {}
    return {"active": bool(active)}


async def ensure_indexes(db) -> None:
    await db[PRODUCTS_COLLECTION].create_index("sku", unique=True)
    await db[PRODUCTS_COLLECTION].create_index("category")
    await db[PRODUCTS_COLLECTION].create_index("active")


async def list_products(
    db,
    *,
    category: Optional[str] = None,
    q: Optional[str] = None,
    active: Optional[bool] = True,
) -> List[Dict[str, Any]]:
    """List products. `active=None` means all; default True hides inactive."""
    query: Dict[str, Any] = _active_filter(active)
    if category:
        query["category"] = category
    if q:
        # Case-insensitive substring match across title/description/tags.
        rx = {"$regex": q, "$options": "i"}
        query["$or"] = [{"title": rx}, {"description": rx}, {"tags": rx}]
    cursor = db[PRODUCTS_COLLECTION].find(query).sort("title", 1)
    return [serialize_product(doc) async for doc in cursor]


async def get_product(db, product_id: str) -> Optional[Dict[str, Any]]:
    try:
        oid = ObjectId(product_id)
    except (InvalidId, TypeError):
        return None
    doc = await db[PRODUCTS_COLLECTION].find_one({"_id": oid})
    return serialize_product(doc) if doc else None


async def create_product(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    doc = dict(payload)
    doc["updated_at"] = datetime.now(timezone.utc)
    try:
        result = await db[PRODUCTS_COLLECTION].insert_one(doc)
    except DuplicateKeyError as exc:
        raise ValueError(f"duplicate sku: {payload.get('sku')}") from exc
    created = await db[PRODUCTS_COLLECTION].find_one({"_id": result.inserted_id})
    log.info("product created", extra={"sku": payload.get("sku")})
    return serialize_product(created)


async def update_product(
    db, product_id: str, patch: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    try:
        oid = ObjectId(product_id)
    except (InvalidId, TypeError):
        return None
    patch = dict(patch)
    patch["updated_at"] = datetime.now(timezone.utc)
    try:
        result = await db[PRODUCTS_COLLECTION].update_one({"_id": oid}, {"$set": patch})
    except DuplicateKeyError as exc:
        raise ValueError(f"duplicate sku: {patch.get('sku')}") from exc
    if result.matched_count == 0:
        return None
    doc = await db[PRODUCTS_COLLECTION].find_one({"_id": oid})
    log.info("product updated", extra={"product_id": product_id})
    return serialize_product(doc)
