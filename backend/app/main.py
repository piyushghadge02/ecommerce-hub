"""FastAPI application factory.

Lifespan:
1. Create PostgreSQL tables if missing (SQLAlchemy metadata.create_all).
2. Create the Elasticsearch `orders` index if missing (graceful if ES down).
3. Start background sync workers per SYNC_STRATEGY:
   - dual_write -> repair loop draining the outbox every 10s
   - outbox     -> outbox worker every OUTBOX_POLL_SECONDS
   - polling    -> polling worker every POLLING_INTERVAL_SECONDS
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import admin, health, orders, products, search, users
from .core.config import Settings, get_settings
from .core.database import (
    close_clients,
    get_engine,
    get_es_client,
    get_mongo_client,
    get_session_factory,
)
from .core.logging import configure_logging, get_logger
from .models.tables import Base
from .repositories import products_repo, search_repo
from .services import sync_service

log = get_logger(__name__)


async def _init_postgres(settings: Settings) -> bool:
    try:
        engine = get_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("postgres tables ensured")
        return True
    except Exception as exc:
        log.warning("postgres init failed", extra={"error": str(exc)})
        return False


async def _init_elasticsearch(settings: Settings) -> bool:
    es = get_es_client()
    if es is None:
        return False
    return await search_repo.ensure_index(es, settings.ES_INDEX)


async def _init_mongo(settings: Settings) -> bool:
    try:
        db = get_mongo_client()[settings.MONGO_DB]
        await products_repo.ensure_indexes(db)
        log.info("mongo indexes ensured")
        return True
    except Exception as exc:
        log.warning("mongo init failed", extra={"error": str(exc)})
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging()

    pg_ok = await _init_postgres(settings)
    es_ok = await _init_elasticsearch(settings)
    mongo_ok = await _init_mongo(settings)
    log.info(
        "startup complete",
        extra={
            "postgres": pg_ok,
            "elasticsearch": es_ok,
            "mongodb": mongo_ok,
            "sync_strategy": settings.SYNC_STRATEGY,
        },
    )

    stop = asyncio.Event()
    tasks: List[asyncio.Task] = []
    if getattr(app.state, "start_workers", True):
        session_factory = get_session_factory(settings)
        es_factory = lambda: get_es_client()  # noqa: E731
        strategy = settings.SYNC_STRATEGY
        if strategy == "dual_write":
            tasks.append(
                asyncio.create_task(
                    sync_service.run_outbox_loop(
                        session_factory,
                        es_factory,
                        settings.ES_INDEX,
                        settings,
                        sync_service.REPAIR_LOOP_SECONDS,
                        stop,
                    )
                )
            )
        elif strategy == "outbox":
            tasks.append(
                asyncio.create_task(
                    sync_service.run_outbox_loop(
                        session_factory,
                        es_factory,
                        settings.ES_INDEX,
                        settings,
                        settings.OUTBOX_POLL_SECONDS,
                        stop,
                    )
                )
            )
        elif strategy == "polling":
            tasks.append(
                asyncio.create_task(
                    sync_service.run_polling_loop(
                        session_factory,
                        es_factory,
                        settings.ES_INDEX,
                        settings.POLLING_INTERVAL_SECONDS,
                        stop,
                    )
                )
            )

    yield

    stop.set()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await close_clients()
    log.info("shutdown complete")


def create_app(settings: Optional[Settings] = None, start_workers: bool = True) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="E-Commerce Order Management & Search Service",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.start_workers = start_workers

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(users.router)
    app.include_router(products.router)
    app.include_router(orders.router)
    app.include_router(search.router)
    app.include_router(health.router)
    app.include_router(admin.router)
    return app


app = create_app()
