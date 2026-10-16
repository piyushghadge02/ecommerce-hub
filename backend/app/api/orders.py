"""Orders API — PostgreSQL is the source of truth; Mongo only validates prices."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.config import Settings, get_settings
from ..core.database import get_es_client, get_mongo_db, get_session_factory
from ..models import schemas
from ..services import order_service

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.post("")
async def create_order(
    payload: schemas.OrderCreate,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    es: Any = Depends(get_es_client),
    settings: Settings = Depends(get_settings),
):
    # Duplicate protection: the same idempotency_key returns the existing order.
    out, status_code = await order_service.place_order(
        get_session_factory(), db, es, settings, payload
    )
    return JSONResponse(status_code=status_code, content=out.model_dump(mode="json"))


@router.get("/{order_id}", response_model=schemas.OrderDetailOut)
async def get_order(
    order_id: int,
    es: Any = Depends(get_es_client),
    settings: Settings = Depends(get_settings),
):
    return await order_service.get_order_detail(
        get_session_factory(), es, settings, order_id
    )


@router.patch("/{order_id}/status", response_model=schemas.StatusUpdateOut)
async def update_status(
    order_id: int,
    payload: schemas.StatusUpdate,
    es: Any = Depends(get_es_client),
    settings: Settings = Depends(get_settings),
):
    return await order_service.update_order_status(
        get_session_factory(), es, settings, order_id, payload.status
    )
