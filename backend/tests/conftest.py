"""Shared fixtures.

One session-scoped event loop drives ALL async code (fixtures, service-layer
calls and ASGI app calls). This avoids cross-loop binding issues with
asyncpg pools and aiohttp sessions.

Unit tests use in-memory fakes (FakeMongoDB / FakeES). Integration tests use
the REAL databases with isolated namespaces:
  - PostgreSQL: the live `ecommerce` DB, wiped before every test (autouse)
  - MongoDB:   a separate `ecommerce_test` database, wiped per test
  - Elasticsearch: a separate `orders_test` index, deleted per test

API calls go through httpx ASGITransport (same loop, no lifespan needed;
tables/indexes are prepared by fixtures).

Run pytest with sanitized proxy env (see TOOLS.md):
  env no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1 .venv/bin/python -m pytest
"""
from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from bson import ObjectId
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings, get_settings
from app.core.database import get_es_client, get_mongo_client, get_mongo_db
from app.main import create_app
from app.models.tables import Base

PG_DSN = "postgresql+asyncpg://ecom:ecom@localhost:5432/ecommerce"
TEST_MONGO_DB = "ecommerce_test"
TEST_ES_INDEX = "orders_test"

# ------------------------------------------------------- session event loop ---
_session_loop = asyncio.new_event_loop()


def arun(coro):
    """Run a coroutine on the shared session event loop."""
    return _session_loop.run_until_complete(coro)


@pytest.fixture(scope="session", autouse=True)
def _loop_lifecycle():
    yield
    _session_loop.run_until_complete(_session_loop.shutdown_asyncgens())
    _session_loop.close()


# ------------------------------------------------------------------ fakes ---
class FakeNotFound(Exception):
    status_code = 404


class FakeCursor:
    def __init__(self, docs: List[Dict[str, Any]]):
        self._docs = docs

    def sort(self, key: str, direction: int = 1):
        self._docs = sorted(
            self._docs, key=lambda d: (d.get(key) is None, d.get(key)),
            reverse=(direction < 0),
        )
        return self

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield deepcopy(d)

        return _gen()


def _match(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
    for key, cond in query.items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in cond):
                return False
        elif isinstance(cond, dict) and "$regex" in cond:
            pattern = cond["$regex"]
            flags = re.IGNORECASE if cond.get("$options") == "i" else 0
            value = doc.get(key)
            if isinstance(value, list):
                if not any(re.search(pattern, str(v), flags) for v in value):
                    return False
            elif not re.search(pattern, str(value or ""), flags):
                return False
        elif isinstance(cond, dict) and "$in" in cond:
            if doc.get(key) not in cond["$in"]:
                return False
        else:
            if doc.get(key) != cond:
                return False
    return True


class FakeMongoCollection:
    def __init__(self):
        self.docs: Dict[ObjectId, Dict[str, Any]] = {}

    def find(self, query: Optional[Dict] = None):
        return FakeCursor([d for d in self.docs.values() if _match(d, query or {})])

    async def find_one(self, filt: Dict):
        for d in self.docs.values():
            if _match(d, filt):
                return deepcopy(d)
        return None

    async def insert_one(self, doc: Dict):
        doc = deepcopy(doc)
        for d in self.docs.values():
            if d.get("sku") == doc.get("sku"):
                from pymongo.errors import DuplicateKeyError

                raise DuplicateKeyError(f"duplicate sku: {doc.get('sku')}")
        doc.setdefault("_id", ObjectId())
        self.docs[doc["_id"]] = doc

        class R:
            inserted_id = doc["_id"]

        return R()

    async def update_one(self, filt: Dict, update: Dict):
        matched = 0
        for oid, d in self.docs.items():
            if _match(d, filt):
                new_sku = update.get("$set", {}).get("sku")
                if new_sku and any(
                    o != oid and v.get("sku") == new_sku for o, v in self.docs.items()
                ):
                    from pymongo.errors import DuplicateKeyError

                    raise DuplicateKeyError(f"duplicate sku: {new_sku}")
                d.update(deepcopy(update.get("$set", {})))
                matched = 1
                break

        class R:
            matched_count = matched

        return R()

    async def delete_many(self, filt: Dict):
        doomed = [o for o, d in self.docs.items() if _match(d, filt or {})]
        for o in doomed:
            del self.docs[o]

        class R:
            deleted_count = len(doomed)

        return R()

    async def count_documents(self, filt: Dict):
        return sum(1 for d in self.docs.values() if _match(d, filt or {}))

    async def create_index(self, *a, **k):
        return "idx"


class FakeMongoDB:
    def __init__(self):
        self._colls: Dict[str, FakeMongoCollection] = {}

    def __getitem__(self, name: str) -> FakeMongoCollection:
        return self._colls.setdefault(name, FakeMongoCollection())


class FakeIndices:
    def __init__(self, parent: "FakeES"):
        self._p = parent

    async def exists(self, index: str):
        return index in self._p._indices

    async def create(self, index: str, **kwargs):
        self._p._indices[index] = kwargs
        self._p._docs.setdefault(index, {})

    async def delete(self, index: str, ignore_unavailable: bool = True):
        self._p._indices.pop(index, None)
        self._p._docs.pop(index, None)

    async def refresh(self, index: Optional[str] = None):
        return None


class FakeES:
    """In-memory stand-in for AsyncElasticsearch (subset of the API we use)."""

    def __init__(self):
        self._indices: Dict[str, Dict] = {}
        self._docs: Dict[str, Dict[str, Dict]] = {}
        self.indices = FakeIndices(self)
        self.down = False
        self.last_search_body: Optional[Dict] = None
        self.index_calls = 0

    async def ping(self):
        return not self.down

    def _guard(self):
        if self.down:
            raise ConnectionError("fake elasticsearch is down")

    async def index(self, index: str, id: str, document: Dict):
        self._guard()
        self.index_calls += 1
        self._docs.setdefault(index, {})[id] = deepcopy(document)
        return {"result": "updated"}

    async def get(self, index: str, id: str):
        self._guard()
        try:
            doc = self._docs[index][id]
        except KeyError:
            raise FakeNotFound(f"document {id} not found")
        return {"_source": deepcopy(doc)}

    async def search(self, index: str, **body):
        self._guard()
        self.last_search_body = deepcopy(body)
        return {
            "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
            "aggregations": {
                "revenue": {"value": 0.0},
                "by_status": {"buckets": []},
                "total_orders": {"value": 0},
            },
        }

    async def count(self, index: str):
        self._guard()
        return {"count": len(self._docs.get(index, {}))}


# ---------------------------------------------------------------- fixtures ---
@pytest.fixture(scope="module")
def pg_engine():
    async def _init():
        engine = create_async_engine(PG_DSN, pool_pre_ping=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine

    engine = arun(_init())
    yield engine
    arun(engine.dispose())


@pytest.fixture(scope="module")
def session_factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def clean_pg(pg_engine):
    async def _wipe():
        async with pg_engine.begin() as conn:
            for table in ("outbox", "order_items", "orders", "sync_state", "users"):
                await conn.execute(text(f"DELETE FROM {table}"))

    arun(_wipe())
    yield


@pytest.fixture()
def fake_mongo():
    return FakeMongoDB()


@pytest.fixture()
def fake_es():
    return FakeES()


@pytest.fixture()
def seeded_mongo(fake_mongo):
    """Fake Mongo preloaded with a small catalog; returns (db, ids)."""
    async def _seed():
        coll = fake_mongo["products"]
        ids = {}
        products = [
            {
                "sku": "WM-001", "title": "Wireless Mouse", "description": "A mouse",
                "price": 50.16, "category": "peripherals", "tags": ["wireless", "usb"],
                "attributes": {"color": "black"},
                "variants": [{"sku": "WM-001-BLK", "color": "black", "stock": 10}],
                "active": True, "updated_at": datetime.now(timezone.utc),
            },
            {
                "sku": "MK-101", "title": "Mechanical Keyboard", "description": "A keyboard",
                "price": 100.34, "category": "peripherals", "tags": ["wired"],
                "attributes": {"switch": "brown"},
                "variants": [{"sku": "MK-101-BRN", "color": "black", "stock": 5}],
                "active": True, "updated_at": datetime.now(timezone.utc),
            },
            {
                "sku": "CRT-1", "title": "Old CRT", "description": "inactive",
                "price": 10.0, "category": "office", "tags": [],
                "attributes": {},
                "variants": [{"sku": "CRT-1", "stock": 1}],
                "active": False, "updated_at": datetime.now(timezone.utc),
            },
        ]
        for p in products:
            r = await coll.insert_one(p)
            ids[p["sku"]] = str(r.inserted_id)
        return ids

    return fake_mongo, arun(_seed())

@pytest.fixture()
def pg_user(session_factory):
    async def _make():
        from app.models.tables import User

        async with session_factory() as session:
            user = User(name="Test User", email="test.user@example.com")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user.id

    return arun(_make())


_UNSET = object()


def make_app(fake_mongo=_UNSET, fake_es=_UNSET,
             strategy: str = "dual_write",
             settings: Optional[Settings] = None):
    """Build the FastAPI app with dependency overrides. Returns (app, settings)."""
    settings = settings or Settings(SYNC_STRATEGY=strategy)
    app = create_app(settings=settings, start_workers=False)
    if fake_mongo is not _UNSET:
        app.dependency_overrides[get_mongo_db] = lambda: fake_mongo
    if fake_es is not _UNSET:
        # pass None explicitly to simulate "elasticsearch unreachable"
        app.dependency_overrides[get_es_client] = lambda: fake_es
    app.dependency_overrides[get_settings] = lambda: settings
    return app, settings


def call(app, method: str, path: str, json=None, params=None) -> Tuple[int, Any]:
    """Synchronous API call through httpx ASGI transport on the session loop."""
    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            resp = await ac.request(method, path, json=json, params=params)
            try:
                body = resp.json()
            except Exception:
                body = None
            return resp.status_code, body

    return arun(_go())


# ------------------------------------------------- integration fixtures ---
@pytest.fixture(scope="module")
def real_es_client():
    """Real AsyncElasticsearch client, used only on the session loop."""
    from elasticsearch import AsyncElasticsearch

    client = AsyncElasticsearch("http://localhost:9200", request_timeout=10)
    yield client
    arun(client.close())


@pytest.fixture()
def itest_settings():
    return Settings(
        MONGO_DB=TEST_MONGO_DB, ES_INDEX=TEST_ES_INDEX, SYNC_STRATEGY="dual_write"
    )


@pytest.fixture()
def real_mongo_db():
    """Motor handle to the isolated `ecommerce_test` database."""
    return get_mongo_client()[TEST_MONGO_DB]


@pytest.fixture(autouse=True)
def clean_test_mongo(real_mongo_db):
    async def _wipe():
        await real_mongo_db["products"].delete_many({})

    arun(_wipe())
    yield
    arun(_wipe())


@pytest.fixture(autouse=True)
def clean_test_es(real_es_client):
    from app.repositories import search_repo

    async def _wipe():
        try:
            await real_es_client.indices.delete(index=TEST_ES_INDEX)
        except Exception:
            pass
        # recreate with the explicit mapping (lifespan does this in prod)
        await search_repo.ensure_index(real_es_client, TEST_ES_INDEX)

    arun(_wipe())
    yield


@pytest.fixture()
def itest_mongo_products(real_mongo_db):
    """Seed the test Mongo DB with a small catalog; returns {sku: id_str}."""
    async def _seed():
        coll = real_mongo_db["products"]
        ids = {}
        products = [
            {
                "sku": "T-WM-001", "title": "Wireless Mouse", "description": "A mouse",
                "price": 50.16, "category": "peripherals", "tags": ["wireless", "usb"],
                "attributes": {"color": "black"},
                "variants": [{"sku": "T-WM-001-BLK", "color": "black", "stock": 10}],
                "active": True, "updated_at": datetime.now(timezone.utc),
            },
            {
                "sku": "T-MK-101", "title": "Mechanical Keyboard", "description": "Keys",
                "price": 100.34, "category": "peripherals", "tags": ["wired"],
                "attributes": {"switch": "brown"},
                "variants": [{"sku": "T-MK-101-BRN", "color": "black", "stock": 5}],
                "active": True, "updated_at": datetime.now(timezone.utc),
            },
            {
                "sku": "T-CRT-1", "title": "Old CRT", "description": "inactive",
                "price": 10.0, "category": "office", "tags": [],
                "attributes": {},
                "variants": [{"sku": "T-CRT-1", "stock": 1}],
                "active": False, "updated_at": datetime.now(timezone.utc),
            },
        ]
        for p in products:
            r = await coll.insert_one(p)
            ids[p["sku"]] = str(r.inserted_id)
        return ids

    return arun(_seed())


def make_itest_app(real_mongo_db, real_es_client, settings: Settings):
    """App wired to the REAL databases (isolated test namespaces)."""
    return make_app(fake_mongo=real_mongo_db, fake_es=real_es_client, settings=settings)
