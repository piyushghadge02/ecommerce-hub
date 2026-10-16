"""Health checks for the app and each database dependency."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ..core.config import Settings, get_settings
from ..core.database import es_ping, get_es_client, get_mongo_client, get_session_factory
from ..models import schemas

router = APIRouter(tags=["health"])


async def _pg_ok() -> bool:
    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _mongo_ok() -> bool:
    try:
        await get_mongo_client().admin.command("ping")
        return True
    except Exception:
        return False


@router.get("/api/health", response_model=schemas.HealthOut)
async def health(
    es: Any = Depends(get_es_client),
    settings: Settings = Depends(get_settings),
):
    pg, mongo, es_up = await _pg_ok(), await _mongo_ok(), await es_ping(es)
    status = "ok" if (pg and mongo and es_up) else "degraded"
    return schemas.HealthOut(
        status=status,
        postgres="up" if pg else "down",
        mongodb="up" if mongo else "down",
        elasticsearch="up" if es_up else "down",
        sync_strategy=settings.SYNC_STRATEGY,
    )
