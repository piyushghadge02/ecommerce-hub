"""Products API — MongoDB ONLY (catalog reads/writes never touch PG/ES)."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_mongo_db
from ..models import schemas
from ..repositories import products_repo

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("", response_model=list[schemas.ProductOut])
async def list_products(
    category: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    active: Literal["true", "false", "all"] = Query(default="true"),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    active_filter = {"true": True, "false": False, "all": None}[active]
    docs = await products_repo.list_products(
        db, category=category, q=q, active=active_filter
    )
    return [schemas.ProductOut(**d) for d in docs]


@router.get("/{product_id}", response_model=schemas.ProductOut)
async def get_product(
    product_id: str, db: AsyncIOMotorDatabase = Depends(get_mongo_db)
):
    doc = await products_repo.get_product(db, product_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"product not found: {product_id}")
    return schemas.ProductOut(**doc)


@router.post("", response_model=schemas.ProductOut, status_code=201)
async def create_product(
    payload: schemas.ProductCreate, db: AsyncIOMotorDatabase = Depends(get_mongo_db)
):
    try:
        doc = await products_repo.create_product(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return schemas.ProductOut(**doc)


@router.put("/{product_id}", response_model=schemas.ProductOut)
async def update_product(
    product_id: str,
    payload: schemas.ProductUpdate,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=422, detail="empty update")
    try:
        doc = await products_repo.update_product(db, product_id, patch)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if doc is None:
        raise HTTPException(status_code=404, detail=f"product not found: {product_id}")
    # Catalog edits touch Mongo only — PG snapshots and ES docs are untouched.
    return schemas.ProductOut(**doc)
