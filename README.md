# E-Commerce Hub

E-Commerce Order Management & Search System

A proof-of-concept e-commerce system where each database does the job it does best:

| Store | Owns | Screens |
|---|---|---|
| **PostgreSQL** | Orders, order items, users, fulfillment status (ACID source of truth) | Checkout, Order Details |
| **MongoDB** | Product catalog (flexible documents: variants, tags, attributes) | Storefront, Catalog Admin |
| **Elasticsearch** | Denormalized order documents + full-text search + KPI aggregations | Admin Search Dashboard |

Built with **FastAPI** (Python 3.11+) and a **Vue 3 + Vite** SPA. No real authentication —
seeded demo users are selected via a "Log In As" dropdown; the app is clearly labeled a POC.

## Quick start

### 1. Prerequisites

- Docker + Docker Compose (for PostgreSQL 16, MongoDB 8, Elasticsearch 8)
- Python 3.11+ and Node 18+

### 2. Start the databases

```bash
docker compose up -d
# wait until healthy:
docker compose ps
curl http://localhost:9200/_cluster/health
```

### 3. Configure

```bash
cp .env.example .env   # defaults work with docker-compose.yml out of the box
```

Key settings: `POSTGRES_DSN`, `MONGO_URI`, `MONGO_DB`, `ELASTICSEARCH_URL`, `ES_INDEX`,
`SYNC_STRATEGY` (`dual_write` | `outbox` | `polling`).

### 4. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m scripts.seed --reset     # create schema, seed PG + Mongo, reindex ES
uvicorn app.main:app --reload --port 8000
```

- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs
- Health: http://localhost:8000/api/health

### 5. Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

The dev server proxies nothing — it calls the API directly at `VITE_API_URL`
(default `http://localhost:8000`; CORS is enabled for `:5173`).

### 6. Seed details

`python -m scripts.seed` is deterministic (seed=42) and reproducible:

- **PostgreSQL:** 8 exact users, 44 orders / 93 order items —
  15 PENDING / 15 PROCESSING / 14 SHIPPED, spread over the last 90 days
  (≥5 older than 60 days, ≥5 in the last 7 days), price bands
  (≥5 under $30, ≥5 $30–$150, ≥5 over $200), every user ≥2 orders,
  Wendy Wireless ≥3 wireless orders, 8+ orders with "Wireless Mouse",
  3+ with "Mechanical Keyboard", 2+ with both. Every total equals its line items.
- **MongoDB:** 25 active products across `peripherals` / `audio` / `cables` / `office`
  (≥5 each; cheap/mid/premium bands; ≥6 wireless) + 1 inactive product (hidden on storefront).
- **Deliberate snapshot mismatch:** after seeding, the Mongo "Wireless Mouse"
  is renamed to **"Wireless Mouse Pro"** ($59.99). PostgreSQL order items and
  Elasticsearch documents keep the original "Wireless Mouse" @ $50.16.
- **Elasticsearch:** every PG order indexed as one denormalized document
  (`scripts/reindex_orders.py` rebuilds the index from PostgreSQL in one command).

Run with `--reset` to drop and recreate demo data; without it the script skips
already-seeded users/products.

## The five screens

| Route | Screen | Reads from | Writes to |
|---|---|---|---|
| `/` | Customer Storefront — product grid, filters, cart drawer | MongoDB | — (cart is frontend state) |
| `/checkout` | Checkout — summary, place order, confirmation | MongoDB (validation) | PostgreSQL |
| `/admin` | Admin Search Dashboard — omni-search, filters, KPIs, paginated table | Elasticsearch only | — |
| `/admin/orders/:id` | Admin Order Details — receipt, status dropdown, sync badge | PostgreSQL (+ ES sync badge) | PostgreSQL → ES |
| `/admin/catalog` | Catalog Administration — product table, create/edit forms | MongoDB | MongoDB only |

## Synchronization strategies

`SYNC_STRATEGY` selects how PostgreSQL orders reach Elasticsearch.
All strategies use the deterministic document id `order-<order_id>`, so retries are
idempotent upserts and reindexing is safe.

1. **`dual_write`** (default) — after the PG transaction commits, the order is indexed
   in Elasticsearch immediately. If ES is unreachable, the order is still committed
   (checkout stays successful) and an `outbox` row is queued; a repair loop drains it.
2. **`outbox`** — an outbox row is inserted *inside the same PG transaction* as the
   order (atomic). A background worker polls the outbox every `OUTBOX_POLL_SECONDS`,
   indexes into ES, and retries with exponential backoff (10 attempts → `failed`,
   visible at `GET /api/admin/sync-status`).
3. **`polling`** — a worker polls `orders` by `updated_at` every
   `POLLING_INTERVAL_SECONDS` (15s) and upserts changed orders; the watermark is
   persisted in `sync_state`.

Demonstrating: place an order on `/checkout`, then watch it appear on `/admin`.
Switch strategies by setting `SYNC_STRATEGY` in `.env` and restarting the API.
To simulate an ES outage, stop the elasticsearch container, place orders (they still
succeed — check `GET /api/admin/sync-status` for the backlog), then start it again
and watch the dashboard catch up. `POST /api/admin/reindex` (or
`python -m scripts.reindex_orders`) rebuilds the whole index from PostgreSQL.

See `NOTES.md` for the split-brain analysis and the answers to the five learning
milestones.

## API summary

| Method & path | Source | Description |
|---|---|---|
| `GET /api/users` | PG | Seeded users for "Log In As" |
| `GET /api/products[?category=&q=&active=]` | Mongo | Catalog (active-only by default) |
| `GET /api/products/{id}` | Mongo | Single product |
| `POST /api/products` | Mongo | Create product |
| `PUT /api/products/{id}` | Mongo | Edit product (never touches order history) |
| `POST /api/orders` | PG (+Mongo validation) | Transactional checkout; supports `idempotency_key` |
| `GET /api/orders/{id}` | PG | Canonical order + ES sync badge |
| `PATCH /api/orders/{id}/status` | PG → ES | Update fulfillment status |
| `POST /api/search/orders` | ES only | Omni-search, filters, sorting, pagination, aggregations |
| `GET /api/health` | all | Dependency health |
| `GET /api/admin/sync-status` | PG + ES | Outbox backlog, watermark, doc count |
| `POST /api/admin/reindex` | PG → ES | Rebuild search index |

## Running tests

```bash
cd backend
pytest -q            # unit + integration (needs the three DBs running)
pytest -q -m "not integration"   # unit tests only (mocked Mongo/ES)
```

33 tests cover: product filtering + inactive hiding, product CRUD validation,
checkout price validation (server prices always win), exact totals, transactional
rollback, idempotent replay, ES-down checkout (PG still commits), status updates,
search across customer names + item titles, status/date/price filters, aggregation
correctness, pagination, snapshot preservation after catalog edits, and all three
sync strategies.

## Evaluator demo script (10 minutes)

1. **Storefront** (`/`): pick "Wendy Wireless" in *Log In As*; note "Wireless Mouse Pro"
   (the renamed catalog product). Add items to the cart.
2. **Checkout** (`/checkout`): review the summary, *Place Order* → note the new order ID.
3. **Search dashboard** (`/admin`): the order appears. Type `wireless` in omni-search —
   it matches both Wendy Wireless's orders (customer name) and orders containing
   wireless products (item titles). Tighten status/date/price filters; watch the KPI
   revenue and per-status counts track the filters exactly.
4. **Order details**: click an order ID → canonical PostgreSQL receipt. Change status
   to SHIPPED, save → return to `/admin` and confirm the new status after sync.
   The sync badge shows whether PG and ES agree.
5. **Catalog admin** (`/admin/catalog`): edit "Wireless Mouse Pro" (e.g. change the price).
   Re-open an old order containing "Wireless Mouse" — it still shows the original
   title and $50.16. History is immutable.
6. **Sync strategies**: set `SYNC_STRATEGY=outbox`, restart the API, place an order —
   it appears on the dashboard after the worker poll. Stop Elasticsearch, place another
   order (checkout still succeeds), restart ES, and watch the dashboard catch up.

## Troubleshooting

- **ES won't start / `vm.max_map_count`**: `sudo sysctl -w vm.max_map_count=262144`
  (needed by the elasticsearch container on Linux).
- **Port conflicts**: change the `ports` in `docker-compose.yml` and the matching
  values in `.env`.
- **Search returns nothing after seeding**: run `python -m scripts.reindex_orders`
  (or `POST /api/admin/reindex`) and check `GET /api/admin/sync-status`.
- **Frontend can't reach the API**: ensure the API runs on `:8000` and
  `VITE_API_URL` matches; CORS allows `http://localhost:5173`.
- **`pip install` proxy errors in this sandbox**: run with
  `no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1` (bracketed IPv6 entries
  in the injected proxy vars break httpx URL parsing).

## Repository layout

```
docker-compose.yml  .env.example  README.md  NOTES.md  PLAN.md
backend/
  app/main.py  app/api/  app/core/  app/models/  app/repositories/  app/services/
  scripts/seed.py  scripts/reindex_orders.py  scripts/schema.sql
  tests/  requirements.txt
frontend/
  src/views/  src/components/  src/api/  src/router/  src/stores/
  package.json  vite.config.js
```
