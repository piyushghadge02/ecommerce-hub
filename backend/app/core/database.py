"""Database client singletons: async PostgreSQL, MongoDB (motor), Elasticsearch.

All clients use short timeouts and the app degrades gracefully when a
dependency is unreachable (health endpoint reports per-dependency status).
"""
from __future__ import annotations

from typing import AsyncIterator, Optional

from elasticsearch import AsyncElasticsearch
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import Settings, get_settings
from .logging import get_logger

log = get_logger(__name__)

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_mongo_client: Optional[AsyncIOMotorClient] = None
_es_client: Optional[AsyncElasticsearch] = None


def get_engine(settings: Optional[Settings] = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(
            settings.POSTGRES_DSN,
            pool_size=5,
            max_overflow=10,
            pool_timeout=5,
            pool_pre_ping=True,
            connect_args={"timeout": 5},
        )
    return _engine


def get_session_factory(settings: Optional[Settings] = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(settings), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


async def get_pg_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one AsyncSession per request."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_mongo_client() -> AsyncIOMotorClient:
    global _mongo_client
    if _mongo_client is None:
        settings = get_settings()
        _mongo_client = AsyncIOMotorClient(
            settings.MONGO_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000
        )
    return _mongo_client


def get_mongo_db() -> AsyncIOMotorDatabase:
    """FastAPI dependency yielding the configured Mongo database."""
    settings = get_settings()
    return get_mongo_client()[settings.MONGO_DB]


def get_es_client() -> Optional[AsyncElasticsearch]:
    """Returns the ES client, or None if the client cannot be constructed.

    Reachability is checked lazily (ping) wherever ES is actually used so a
    down Elasticsearch never prevents app startup.
    """
    global _es_client
    if _es_client is None:
        settings = get_settings()
        try:
            _es_client = AsyncElasticsearch(
                settings.ELASTICSEARCH_URL,
                request_timeout=5,
                retry_on_timeout=False,
                max_retries=0,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("could not construct elasticsearch client", extra={"error": str(exc)})
            return None
    return _es_client


async def es_ping(es: Optional[AsyncElasticsearch]) -> bool:
    if es is None:
        return False
    try:
        return bool(await es.ping())
    except Exception as exc:
        log.warning("elasticsearch ping failed", extra={"error": str(exc)})
        return False


async def close_clients() -> None:
    global _engine, _mongo_client, _es_client, _session_factory
    if _es_client is not None:
        try:
            await _es_client.close()
        except Exception:
            pass
        _es_client = None
    if _mongo_client is not None:
        _mongo_client.close()
        _mongo_client = None
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
