#!/usr/bin/env python3
"""Rebuild the Elasticsearch `orders` index from PostgreSQL.

Reads orders + users + order_items from PostgreSQL and upserts denormalized
documents (historical title/price snapshots) into Elasticsearch with
deterministic ids `order-<order_id>`. The MongoDB catalog is NEVER consulted:
historical snapshots must survive catalog edits.

Usage:
    cd backend && .venv/bin/python scripts/reindex_orders.py [--reset]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.core.database import close_clients, get_es_client, get_session_factory
from app.core.logging import configure_logging, get_logger
from app.repositories import search_repo
from app.services import sync_service

log = get_logger("reindex")


async def main(reset: bool = False) -> int:
    configure_logging()
    settings = get_settings()
    es = get_es_client()
    if es is None:
        log.error("elasticsearch client unavailable; aborting")
        return 1
    try:
        if not await search_repo.ensure_index(es, settings.ES_INDEX):
            log.error("elasticsearch unreachable; aborting")
            return 1
        if reset:
            await search_repo.delete_index(es, settings.ES_INDEX)
            await search_repo.ensure_index(es, settings.ES_INDEX)
        indexed = await sync_service.reindex_all(
            get_session_factory(settings), es, settings.ES_INDEX
        )
        print(f"reindexed {indexed} orders into '{settings.ES_INDEX}'")
        return 0
    finally:
        await close_clients()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete the index first")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(reset=args.reset)))
