"""Search API — Elasticsearch ONLY. Never PostgreSQL or MongoDB."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..core.config import Settings, get_settings
from ..core.database import get_es_client
from ..models import schemas
from ..repositories import search_repo

router = APIRouter(prefix="/api/search", tags=["search"])


@router.post("/orders", response_model=schemas.SearchResponse)
async def search_orders(
    payload: schemas.SearchRequest,
    es: Any = Depends(get_es_client),
    settings: Settings = Depends(get_settings),
):
    if es is None:
        raise HTTPException(
            status_code=503, detail="search unavailable: elasticsearch unreachable"
        )
    try:
        return await search_repo.search_orders(es, settings.ES_INDEX, payload)
    except HTTPException:
        raise
    except Exception as exc:
        # ES connection/auth/index errors must surface as 503 (retryable),
        # never as an unhandled 500 which the dashboard shows as "Search failed".
        raise HTTPException(
            status_code=503,
            detail=f"search unavailable: {type(exc).__name__}: {exc}".strip()[:300],
        ) from exc
