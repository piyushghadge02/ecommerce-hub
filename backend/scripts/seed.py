#!/usr/bin/env python3
"""Deterministic demo seed (seed=42) for PG + MongoDB + Elasticsearch.

Seeds:
  * 8 users (exact names/emails from the spec)
  * 26 products (25 active + 1 inactive) across peripherals/audio/cables/office
  * 44 orders / >=80 items meeting every fixture, band, date and status rule
  * deliberate snapshot mismatch: "Wireless Mouse" -> "Wireless Mouse Pro" (59.99)
    in Mongo AFTER orders are written; PG/ES history keeps the old snapshot
  * full Elasticsearch reindex from PostgreSQL

Usage:
    cd backend && .venv/bin/python scripts/seed.py [--reset]

--reset wipes PG transactional tables, the Mongo products collection and the
ES index first. Without --reset, existing users/products are skipped (orders
are only seeded when the orders table is empty).
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.core.database import (
    close_clients,
    get_engine,
    get_es_client,
    get_mongo_client,
    get_session_factory,
)
from app.core.logging import configure_logging, get_logger
from app.models.tables import Base
from app.repositories import orders_repo, products_repo, search_repo
from app.services import sync_service
from sqlalchemy import text

log = get_logger("seed")

SEED = 42
NOW = datetime.now(timezone.utc)

USERS: List[Tuple[str, str]] = [
    ("John Doe", "john.doe@example.com"),
    ("Jane Smith", "jane.smith@example.com"),
    ("Wendy Wireless", "wendy.wireless@example.com"),
    ("Alex Rivera", "alex.rivera@example.com"),
    ("Sam Patel", "sam.patel@example.com"),
    ("Casey Nguyen", "casey.nguyen@example.com"),
    ("Morgan Lee", "morgan.lee@example.com"),
    ("Riley Brooks", "riley.brooks@example.com"),
]
WENDY_IDX = 2


def _product(
    sku: str,
    title: str,
    price: float,
    category: str,
    tags: List[str],
    attributes: Dict[str, Any],
    variants: List[Dict[str, Any]],
    description: str = "",
    active: bool = True,
) -> Dict[str, Any]:
    return {
        "sku": sku,
        "title": title,
        "description": description or f"{title} — quality {category} product.",
        "price": price,
        "category": category,
        "tags": tags,
        "attributes": attributes,
        "variants": variants,
        "active": active,
        "updated_at": NOW,
    }


def _v(sku: str, color: str, stock: int) -> Dict[str, Any]:
    return {"sku": sku, "color": color, "stock": stock}


PRODUCTS: List[Dict[str, Any]] = [
    # ---- peripherals (7 active) ----
    _product("WM-001", "Wireless Mouse", 50.16, "peripherals",
             ["wireless", "usb", "office"], {"color": "black", "dpi": 1600, "battery": "AA"},
             [_v("WM-001-BLK", "black", 40), _v("WM-001-WHT", "white", 12)]),
    _product("MK-101", "Mechanical Keyboard", 100.34, "peripherals",
             ["wired", "rgb", "gaming"], {"switch": "brown", "layout": "TKL", "backlight": True},
             [_v("MK-101-BRN", "black", 25)]),
    _product("WK-102", "Wireless Keyboard", 65.00, "peripherals",
             ["wireless", "bluetooth"], {"layout": "full", "battery": "rechargeable"},
             [_v("WK-102-BLK", "black", 30)]),
    _product("HUB-201", "USB-C Hub 7-in-1", 45.00, "peripherals",
             ["usb-c", "hub", "laptop"], {"ports": 7, "hdmi": "4K"},
             [_v("HUB-201-GRY", "gray", 50)]),
    _product("MP-202", "Ergonomic Mouse Pad", 18.99, "peripherals",
             ["office", "desk"], {"size": "XL", "material": "rubber"},
             [_v("MP-202-BLK", "black", 100)]),
    _product("CAM-203", "Webcam HD 1080p", 55.50, "peripherals",
             ["video", "usb"], {"resolution": "1080p", "microphone": True},
             [_v("CAM-203-BLK", "black", 35)]),
    _product("MON-204", "4K Monitor 27-inch", 249.99, "peripherals",
             ["display", "4k", "usb-c"], {"size": "27in", "panel": "IPS"},
             [_v("MON-204-BLK", "black", 15)]),
    # ---- audio (6 active) ----
    _product("EB-301", "Wireless Earbuds", 79.99, "audio",
             ["wireless", "bluetooth", "audio"], {"battery_hours": 24, "anc": False},
             [_v("EB-301-WHT", "white", 60), _v("EB-301-BLK", "black", 45)]),
    _product("NCH-302", "Noise Cancelling Headphones", 199.00, "audio",
             ["wireless", "anc", "premium"], {"battery_hours": 40, "anc": True},
             [_v("NCH-302-BLK", "black", 20)]),
    _product("SPK-303", "Bluetooth Speaker Mini", 35.75, "audio",
             ["wireless", "bluetooth", "speaker"], {"battery_hours": 12, "waterproof": "IPX5"},
             [_v("SPK-303-BLU", "blue", 40)]),
    _product("MIC-304", "USB Microphone Pro", 89.00, "audio",
             ["usb", "streaming"], {"pattern": "cardioid", "mount": "desk"},
             [_v("MIC-304-BLK", "black", 28)]),
    _product("EP-305", "Wired Earphones", 12.99, "audio",
             ["wired", "budget"], {"connector": "3.5mm"},
             [_v("EP-305-WHT", "white", 120)]),
    _product("SB-306", "Soundbar Slim", 129.99, "audio",
             ["tv", "hdmi", "audio"], {"channels": "2.1", "hdmi_arc": True},
             [_v("SB-306-BLK", "black", 18)]),
    # ---- cables (6 active) ----
    _product("CBL-401", "USB-C to USB-C Cable 2m", 14.99, "cables",
             ["usb-c", "charging"], {"length": "2m", "wattage": 100},
             [_v("CBL-401-BLK", "black", 200)]),
    _product("HDMI-402", "HDMI 2.1 Cable", 19.99, "cables",
             ["hdmi", "4k"], {"length": "2m", "version": "2.1"},
             [_v("HDMI-402-BLK", "black", 150)]),
    _product("WCP-403", "Wireless Charging Pad", 29.99, "cables",
             ["wireless", "charging", "qi"], {"wattage": 15},
             [_v("WCP-403-WHT", "white", 70)]),
    _product("LGT-404", "Lightning Cable 1m", 11.50, "cables",
             ["apple", "charging"], {"length": "1m", "mfi": True},
             [_v("LGT-404-WHT", "white", 180)]),
    _product("DP-405", "DisplayPort Cable", 22.00, "cables",
             ["display", "pc"], {"length": "1.8m", "version": "1.4"},
             [_v("DP-405-BLK", "black", 90)]),
    _product("ETH-406", "Ethernet Cable Cat6 5m", 16.75, "cables",
             ["network", "wired"], {"length": "5m", "category": "Cat6"},
             [_v("ETH-406-BLU", "blue", 110)]),
    # ---- office (6 active + 1 inactive) ----
    _product("LAMP-501", "Desk Lamp LED", 32.50, "office",
             ["desk", "lighting"], {"dimmable": True, "color_temp": "3000-6000K"},
             [_v("LAMP-501-WHT", "white", 55)]),
    _product("MST-502", "Monitor Stand Bamboo", 49.99, "office",
             ["desk", "ergonomic"], {"material": "bamboo", "max_load_kg": 15},
             [_v("MST-502-NAT", "natural", 32)]),
    _product("PRS-503", "Wireless Presenter Remote", 39.95, "office",
             ["wireless", "presentation"], {"range_m": 30, "laser": "red"},
             [_v("PRS-503-BLK", "black", 48)]),
    _product("LST-504", "Laptop Stand Aluminum", 42.00, "office",
             ["desk", "laptop"], {"material": "aluminum", "adjustable": True},
             [_v("LST-504-SLV", "silver", 42)]),
    _product("ORG-505", "Desk Organizer Set", 24.99, "office",
             ["desk", "storage"], {"pieces": 5},
             [_v("ORG-505-GRY", "gray", 65)]),
    _product("CUSH-506", "Office Chair Cushion", 27.50, "office",
             ["comfort", "chair"], {"material": "memory foam"},
             [_v("CUSH-506-BLK", "black", 80)]),
    _product("CRT-507", "Old CRT Monitor", 99.99, "office",
             ["vintage"], {"size": "17in"}, [_v("CRT-507-BGE", "beige", 2)],
             active=False),
]


# ---------------------------------------------------------------------------
# Order plan (pure function — unit-testable without any database)
# ---------------------------------------------------------------------------
def _idx_by_sku(products: List[Dict[str, Any]]) -> Dict[str, int]:
    return {p["sku"]: i for i, p in enumerate(products)}


def build_order_plan(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return 44 order dicts: {user_idx, lines=[(prod_idx, qty)], status, days_ago}.

    Satisfies every §9 rule deterministically (rng seed 42).
    """
    rng = random.Random(SEED)
    sku = _idx_by_sku(products)
    active = [i for i, p in enumerate(products) if p.get("active", True)]
    wireless = [
        i for i, p in enumerate(products)
        if p.get("active", True)
        and ("wireless" in p["title"].lower() or "wireless" in [t.lower() for t in p["tags"]])
    ]
    cheap = [i for i in active if products[i]["price"] < 25]
    mid = [i for i in active if 25 <= products[i]["price"] <= 99]

    WM, MK = sku["WM-001"], sku["MK-101"]
    NCH, SB, MON = sku["NCH-302"], sku["SB-306"], sku["MON-204"]
    EP = sku["EP-305"]

    plan: List[Dict[str, Any]] = []

    def add_order(user_idx: int, lines: List[Tuple[int, int]]) -> int:
        plan.append({"user_idx": user_idx, "lines": lines, "status": None, "days_ago": None})
        return len(plan) - 1

    def random_lines(n: int) -> List[Tuple[int, int]]:
        return [(rng.choice(active), rng.randint(1, 3)) for _ in range(n)]

    # 1) base: every user gets 2 orders (slots 0..15); >=80 items total required
    for u in range(8):
        for _ in range(2):
            add_order(u, random_lines(rng.choice([2, 2, 3])))
    # 2) Wendy's 3 extra wireless orders (slots 16..18) — each gets Wireless Mouse
    for _ in range(3):
        add_order(WENDY_IDX, [(WM, rng.randint(1, 2))] + random_lines(rng.randint(0, 1)))
    # 3) price bands (slots 19..33)
    for i in range(5):  # under $30
        add_order(rng.randrange(8), [(cheap[i % len(cheap)], 1)])
    for i in range(5):  # $30..$150
        p = mid[i % len(mid)]
        qty = 1 if products[p]["price"] * 2 > 150 else 2
        add_order(rng.randrange(8), [(p, qty)])
    premium_lines = [
        [(NCH, 2)], [(SB, 2)], [(MON, 1)], [(MK, 3)], [(NCH, 1), (EP, 1)],
    ]
    for lines in premium_lines:  # over $200
        add_order(rng.randrange(8), lines)
    # 4) random fill (slots 34..43)
    for _ in range(10):
        add_order(rng.randrange(8), random_lines(rng.choice([2, 2, 3])))

    assert len(plan) == 44, len(plan)

    # 5) fixtures: 8 orders contain Wireless Mouse, 3 contain Mechanical
    #    Keyboard, 2 contain both. Slots 16..18 already have WM (Wendy).
    fixture_pool = list(range(34, 44))
    rng.shuffle(fixture_pool)
    wm_slots = [16, 17, 18] + fixture_pool[:5]
    mk_slots = fixture_pool[5:8]
    both_slots = fixture_pool[:2]  # 2 of the WM slots also get MK
    for s in wm_slots:
        if not any(pi == WM for pi, _ in plan[s]["lines"]):
            plan[s]["lines"].append((WM, 1))
    for s in mk_slots:
        plan[s]["lines"].append((MK, 1))
    for s in both_slots:
        if not any(pi == MK for pi, _ in plan[s]["lines"]):
            plan[s]["lines"].append((MK, 1))

    # 6) statuses 15/15/14
    for i, o in enumerate(plan):
        o["status"] = "PENDING" if i < 15 else ("PROCESSING" if i < 30 else "SHIPPED")

    # 7) dates: 5 within last 7 days, >=5 older than 60 days, spread over 90
    days_ago = [1, 2, 4, 5, 6] + [8 + (i * 82) // 38 for i in range(39)]
    rng.shuffle(days_ago)
    for o, d in zip(plan, days_ago):
        o["days_ago"] = d

    return plan


def check_plan(plan: List[Dict[str, Any]], products: List[Dict[str, Any]]) -> List[str]:
    """Return a list of violated §9 rules (empty == all satisfied)."""
    problems: List[str] = []
    sku = _idx_by_sku(products)
    WM, MK = sku["WM-001"], sku["MK-101"]

    def total(o: Dict[str, Any]) -> Decimal:
        return sum(
            (Decimal(str(products[pi]["price"])) * qty for pi, qty in o["lines"]),
            Decimal("0"),
        )

    if len(plan) != 44:
        problems.append(f"expected 44 orders, got {len(plan)}")
    n_items = sum(len(o["lines"]) for o in plan)
    if n_items < 80:
        problems.append(f"expected >=80 items, got {n_items}")
    statuses = [o["status"] for o in plan]
    for st, need in (("PENDING", 10), ("PROCESSING", 10), ("SHIPPED", 10)):
        if statuses.count(st) < need:
            problems.append(f"status {st}: {statuses.count(st)} < {need}")
    old = sum(1 for o in plan if o["days_ago"] > 60)
    if old < 5:
        problems.append(f"orders older than 60 days: {old} < 5")
    recent = sum(1 for o in plan if o["days_ago"] <= 7)
    if recent < 5:
        problems.append(f"orders within last 7 days: {recent} < 5")
    if max(o["days_ago"] for o in plan) < 85:
        problems.append("dates do not spread over ~90 days")
    totals = [total(o) for o in plan]
    if sum(1 for t in totals if t < 30) < 5:
        problems.append("need >=5 orders under $30")
    if sum(1 for t in totals if 30 <= t <= 150) < 5:
        problems.append("need >=5 orders $30-$150")
    if sum(1 for t in totals if t > 200) < 5:
        problems.append("need >=5 orders over $200")
    per_user = [sum(1 for o in plan if o["user_idx"] == u) for u in range(8)]
    if min(per_user) < 2:
        problems.append(f"every user needs >=2 orders: {per_user}")
    wendy_wireless = sum(
        1 for o in plan if o["user_idx"] == WENDY_IDX
        and any("wireless" in products[pi]["title"].lower()
                or "wireless" in [t.lower() for t in products[pi]["tags"]]
                for pi, _ in o["lines"])
    )
    if wendy_wireless < 3:
        problems.append(f"Wendy wireless orders: {wendy_wireless} < 3")
    wm_orders = sum(1 for o in plan if any(pi == WM for pi, _ in o["lines"]))
    if wm_orders < 8:
        problems.append(f"Wireless Mouse orders: {wm_orders} < 8")
    mk_orders = sum(1 for o in plan if any(pi == MK for pi, _ in o["lines"]))
    if mk_orders < 3:
        problems.append(f"Mechanical Keyboard orders: {mk_orders} < 3")
    both = sum(
        1 for o in plan
        if any(pi == WM for pi, _ in o["lines"]) and any(pi == MK for pi, _ in o["lines"])
    )
    if both < 2:
        problems.append(f"orders with both products: {both} < 2")
    return problems


# ---------------------------------------------------------------------------
# Database execution
# ---------------------------------------------------------------------------
async def seed_users(session_factory) -> Dict[int, int]:
    """Insert the 8 users; return {plan_user_idx -> db id}."""
    from sqlalchemy import select
    from app.models.tables import User

    mapping: Dict[int, int] = {}
    async with session_factory() as session:
        for idx, (name, email) in enumerate(USERS):
            existing = (
                await session.execute(select(User).where(User.email == email))
            ).scalars().first()
            if existing is None:
                existing = User(name=name, email=email)
                session.add(existing)
                await session.flush()
            mapping[idx] = existing.id
        await session.commit()
    return mapping


async def seed_products(db, reset: bool) -> List[Dict[str, Any]]:
    coll = db[products_repo.PRODUCTS_COLLECTION]
    if reset:
        await coll.delete_many({})
    await products_repo.ensure_indexes(db)
    docs: List[Dict[str, Any]] = []
    for p in PRODUCTS:
        existing = await coll.find_one({"sku": p["sku"]})
        if existing is None:
            result = await coll.insert_one(dict(p))
            existing = await coll.find_one({"_id": result.inserted_id})
        docs.append(existing)
    return docs


async def seed_orders(session_factory, user_map: Dict[int, int],
                      products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from sqlalchemy import select, func
    from app.models.tables import Order

    async with session_factory() as session:
        existing = (await session.execute(select(func.count(Order.id)))).scalar()
    if existing:
        log.info("orders already seeded; skipping", extra={"count": existing})
        return []

    plan = build_order_plan(products)
    problems = check_plan(plan, products)
    if problems:
        raise RuntimeError(f"seed plan violates rules: {problems}")

    # product lookup by plan index -> (mongo _id str, title, Decimal price)
    prod_refs = [
        (str(p["_id"]), p["title"], Decimal(str(p["price"])).quantize(Decimal("0.01")))
        for p in products
    ]

    async with session_factory() as session:
        count = (await session.execute(select(func.count(Order.id)))).scalar()
    if count:
        return []

    order_ids: List[int] = []
    for o in plan:
        lines = [
            (prod_refs[pi][0], prod_refs[pi][1], qty, prod_refs[pi][2])
            for pi, qty in o["lines"]
        ]
        order_date = NOW - timedelta(days=o["days_ago"], minutes=(o["days_ago"] * 37) % 1440)
        async with session_factory() as session:
            order, _ = await orders_repo.create_order(
                session,
                user_id=user_map[o["user_idx"]],
                lines=lines,
                order_date=order_date,
                updated_at=order_date,
            )
            # create_order defaults status to PENDING; fix to the planned one.
            if o["status"] != "PENDING":
                await orders_repo.update_order_status(session, order.id, o["status"])
            order_ids.append(order.id)
    log.info("orders seeded", extra={"count": len(order_ids)})
    return plan


async def apply_snapshot_mismatch(db) -> None:
    """Rename Wireless Mouse -> Wireless Mouse Pro + new price (Mongo only)."""
    coll = db[products_repo.PRODUCTS_COLLECTION]
    result = await coll.update_one(
        {"sku": "WM-001"},
        {"$set": {"title": "Wireless Mouse Pro", "price": 59.99,
                  "updated_at": datetime.now(timezone.utc)}},
    )
    log.info("snapshot mismatch applied", extra={"matched": result.matched_count})


async def verify(session_factory, db, es, settings) -> None:
    from sqlalchemy import select, func
    from app.models.tables import Order, OrderItem, User

    async with session_factory() as session:
        n_users = (await session.execute(select(func.count(User.id)))).scalar()
        n_orders = (await session.execute(select(func.count(Order.id)))).scalar()
        n_items = (await session.execute(select(func.count(OrderItem.id)))).scalar()
    coll = db[products_repo.PRODUCTS_COLLECTION]
    n_products = await coll.count_documents({"active": True})
    n_inactive = await coll.count_documents({"active": False})
    wm = await coll.find_one({"sku": "WM-001"})
    es_docs = await search_repo.count_docs(es, settings.ES_INDEX) if es else None

    # historical snapshot check: PG/ES must still show the ORIGINAL title/price
    async with session_factory() as session:
        old = (
            await session.execute(
                select(OrderItem.title, OrderItem.unit_price)
                .where(OrderItem.title == "Wireless Mouse")
                .limit(1)
            )
        ).first()

    print(f"users={n_users} products(active)={n_products} inactive={n_inactive} "
          f"orders={n_orders} items={n_items} es_docs={es_docs}")
    print(f"mongo WM-001 title={wm['title']!r} price={wm['price']}")
    print(f"historical PG snapshot intact: {old is not None} "
          f"{(old[0], float(old[1])) if old else ''}")
    assert n_users == 8, "expected 8 users"
    assert n_products >= 24, "expected >=24 active products"
    assert n_inactive >= 1, "expected >=1 inactive product"
    assert n_orders == 44, "expected 44 orders"
    assert n_items >= 80, "expected >=80 items"
    assert wm["title"] == "Wireless Mouse Pro" and float(wm["price"]) == 59.99
    assert old is not None and old[0] == "Wireless Mouse" \
        and float(old[1]) == 50.16, "historical snapshot was rewritten!"
    if es_docs is not None:
        assert es_docs == n_orders, f"ES docs {es_docs} != PG orders {n_orders}"
    print("seed verification passed")


async def main(reset: bool = False) -> int:
    configure_logging()
    settings = get_settings()
    session_factory = get_session_factory(settings)

    async with get_engine(settings).begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if reset:
        async with session_factory() as session:
            await orders_repo.delete_all_demo_data(session)
        log.info("postgres demo data wiped")

    mongo_client = get_mongo_client()
    db = mongo_client[settings.MONGO_DB]

    user_map = await seed_users(session_factory)
    products = await seed_products(db, reset)
    await seed_orders(session_factory, user_map, products)
    await apply_snapshot_mismatch(db)

    es = get_es_client()
    if reset:
        # drop the index so stale docs from a previous seed can't linger
        await search_repo.delete_index(es, settings.ES_INDEX)
    if not await search_repo.ensure_index(es, settings.ES_INDEX):
        print("ERROR: elasticsearch unreachable — PG/Mongo seeded, ES reindex skipped")
        await close_clients()
        return 1
    indexed = await sync_service.reindex_all(session_factory, es, settings.ES_INDEX)
    print(f"reindexed {indexed} orders")

    await verify(session_factory, db, es, settings)
    await close_clients()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="wipe demo data first")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(reset=args.reset)))
