# Implementation Plan — E-Commerce Hub (E-Commerce Order Management & Search System)

Source of truth: `~/workspace/user/files/ecommerce-postgres-mongo-es.pdf`.
Repo root: `~/workspace/ecommerce-polyglot-order-management/`
GitHub target: `ecommerce-polyglot-order-management` (public, branch `main`).

## 1. Tech stack (locked)

- Backend: Python 3.11+, FastAPI, Uvicorn, Pydantic v2 + pydantic-settings, SQLAlchemy 2.x
  (async via `asyncpg`), PyMongo (use `motor` for async Mongo), official `elasticsearch` client,
  pytest + httpx.
- Frontend: Vue 3 (Composition API), Vite, Vue Router, Tailwind CSS, `lucide-vue-next` icons.
- Databases: PostgreSQL 16 (orders/users), MongoDB 8 (catalog), Elasticsearch 8 (search).
- `docker-compose.yml` brings up all three DBs. API + frontend run on host by default (documented).

## 2. Repository layout

```
repo/
  docker-compose.yml
  .env.example
  .gitignore
  README.md
  NOTES.md
  PLAN.md                      # this file
  backend/
    requirements.txt
    app/
      main.py                  # FastAPI app factory, CORS, lifespan (worker startup)
      core/
        config.py              # pydantic-settings
        logging.py             # structured logging
        database.py            # PG engine/session, Mongo client, ES client singletons
      models/
        schemas.py             # Pydantic request/response DTOs
        tables.py              # SQLAlchemy 2.x table definitions
      repositories/
        users_repo.py          # PG: users
        orders_repo.py         # PG: orders + order_items + outbox + sync_state
        products_repo.py       # Mongo: products
        search_repo.py         # ES: orders index search + index ops
      services/
        order_service.py       # checkout transaction, status update
        sync_service.py        # dual-write / outbox worker / polling worker
      api/
        products.py            # GET /api/products, GET /api/products/{id}, POST, PUT
        orders.py              # POST /api/orders, GET /api/orders/{id}, PATCH /api/orders/{id}/status
        search.py              # POST /api/search/orders
        users.py               # GET /api/users
        health.py              # GET /api/health
        admin.py               # GET /api/admin/sync-status (outbox backlog), POST /api/admin/reindex
    scripts/
      seed.py                  # full deterministic seed (PG + Mongo + ES)
      reindex_orders.py        # rebuild ES `orders` index from PG
    tests/
      conftest.py
      test_products.py
      test_orders.py           # price validation, totals, rollback, idempotency
      test_search.py           # mocked ES + integration
      test_sync.py             # dual-write failure, outbox worker
  frontend/
    package.json
    vite.config.js
    index.html
    src/
      main.js, App.vue
      router/index.js          # /, /checkout, /admin, /admin/orders/:id, /admin/catalog
      api/client.js            # axios/fetch wrapper, base URL from VITE_API_URL
      stores/                  # cart store, user store (simple reactive)
      components/              # ProductCard, CartDrawer, StatusBadge, KpiCard, Pagination, Toast, Skeletons...
      views/
        Storefront.vue         # Screen 1 (MongoDB)
        Checkout.vue           # Screen 2 (PG + Mongo validation)
        AdminDashboard.vue     # Screen 3 (ES only)
        OrderDetails.vue       # Screen 4 (PG + ES sync badge)
        CatalogAdmin.vue       # Screen 5 (Mongo)
      assets/main.css          # Tailwind + design tokens
```

## 3. Environment variables (.env.example)

```
POSTGRES_DSN=postgresql+asyncpg://ecom:ecom@localhost:5432/ecommerce
MONGO_URI=mongodb://localhost:27017
MONGO_DB=ecommerce
ELASTICSEARCH_URL=http://localhost:9200
ES_INDEX=orders
SYNC_STRATEGY=dual_write        # dual_write | outbox | polling
OUTBOX_POLL_SECONDS=2
POLLING_INTERVAL_SECONDS=15
API_HOST=0.0.0.0
API_PORT=8000
VITE_API_URL=http://localhost:8000
```

## 4. API contract (exact)

Base `http://localhost:8000`. All JSON. Error envelope: `{"detail": "..."}` (+ optional `code`).

### Users (PostgreSQL)
- `GET /api/users` → `[{id, name, email}]` (8 seeded users; "Log In As" dropdown)

### Products (MongoDB ONLY)
- `GET /api/products?category=&q=&active=true`
  - `active`: `true` (default) | `false` | `all`. Storefront omits → active only.
  - `q`: case-insensitive substring on title/description/tags (Mongo regex).
  - → `[{id, sku, title, description, price, category, tags[], attributes{}, variants[{sku,color,stock,...}], active, updated_at}]`
- `GET /api/products/{id}` → single product or 404
- `POST /api/products` → body `{sku,title,description,price,category,tags[],attributes{},variants[],active}` → 201 created doc. Validate: title/sku/price required, price > 0, ≥1 variant.
- `PUT /api/products/{id}` → partial/full update → 200. Must NOT touch PG order_items or ES docs.

### Orders (PostgreSQL; Mongo used only for price/title validation)
- `POST /api/orders` body:
  ```json
  {"user_id": 3, "items": [{"product_id": "<mongo _id hex>", "quantity": 2}], "idempotency_key": "uuid-optional"}
  ```
  Behavior:
  1. Validate user exists (400 otherwise).
  2. Validate each product_id exists in Mongo AND active=true (422 if inactive/missing).
  3. Validate quantity ≥ 1 integer (422).
  4. Server computes totals from Mongo price (Decimal). Client prices ignored.
  5. One PG transaction: INSERT orders (status=PENDING) + INSERT order_items (product_id TEXT, title snapshot, unit_price NUMERIC(12,2), quantity) + optional idempotency_key (UNIQUE, nullable).
     Roll back everything on any failure.
  6. After commit → sync to ES per SYNC_STRATEGY (see §7).
  7. Idempotency: if idempotency_key already exists → return existing order with 200.
  - → 201 `{"order_id","user_id","status","total_amount","order_date","updated_at","items":[{product_id,title,quantity,unit_price,line_total}],"es_sync":{"strategy","status"}}`
    where `es_sync.status` ∈ `synced | pending_retry | queued`.
- `GET /api/orders/{id}` → canonical PG order:
  `{"order_id","user_id","customer":{"id","name","email"},"status","total_amount","order_date","updated_at","items":[...],"sync":{"es_in_sync": true|false|null, "pg_updated_at": "...", "es_updated_at": "..."|null}}`
  `es_in_sync` compares second-truncated updated_at; null if ES unreachable.
- `PATCH /api/orders/{id}/status` body `{"status": "PROCESSING"}` → validate ∈ {PENDING,PROCESSING,SHIPPED} (422 otherwise); update PG `status`+`updated_at`; then ES sync per strategy. → 200 `{"order_id","status","updated_at","es_sync":{...}}`.

### Search (Elasticsearch ONLY — never PG/Mongo)
- `POST /api/search/orders` body:
  ```json
  {"q": "wireless", "statuses": ["PENDING","SHIPPED"], "date_from": "2026-07-01", "date_to": "2026-10-16",
   "min_total": 30, "max_total": 150, "sort_by": "order_date", "sort_dir": "desc", "page": 1, "page_size": 20}
  ```
  - `q`: multi_match across `customer.name`, `customer.email`, `items.title` (+ nested query for items.title); partial terms like "wireless" must match (use match with default analyzer — "Wireless Mouse" tokenizes to "wireless").
  - Filters: terms on `status` (keyword), range on `order_date`, range on `total_amount`.
  - Sorting: `order_date | total_amount | order_id`, dir asc/desc.
  - Pagination: page ≥ 1, page_size ≤ 100.
  - Aggregations (same filter context as hits): `sum(total_amount)` → revenue; `terms(status)` → counts; `value_count` → total.
  - → `{"total","page","page_size","total_pages","orders":[{"order_id","order_date","customer":{"id","name","email"},"total_amount","status"}],"aggs":{"revenue","by_status":{"PENDING":n,"PROCESSING":n,"SHIPPED":n},"total_orders"}}`

### Admin / health
- `GET /api/health` → `{"status":"ok|degraded","postgres":"up|down","mongodb":"up|down","elasticsearch":"up|down","sync_strategy":"..."}`
- `GET /api/admin/sync-status` → `{"outbox_pending": n, "outbox_failed": n, "polling_watermark": "..."|null, "elasticsearch_docs": n}`.
- `POST /api/admin/reindex` → rebuilds ES from PG (same code as scripts/reindex_orders.py) → `{"indexed": n}`.

## 5. PostgreSQL schema

```sql
users(id SERIAL PK, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE);
orders(id SERIAL PK, user_id INT NOT NULL REFERENCES users(id),
       order_date TIMESTAMPTZ NOT NULL DEFAULT now(),
       status TEXT NOT NULL CHECK (status IN ('PENDING','PROCESSING','SHIPPED')),
       total_amount NUMERIC(12,2) NOT NULL CHECK (total_amount >= 0),
       updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
       idempotency_key TEXT UNIQUE);
order_items(id SERIAL PK, order_id INT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_id TEXT NOT NULL,          -- Mongo ObjectId as text
            title TEXT NOT NULL,               -- snapshot
            quantity INT NOT NULL CHECK (quantity >= 1),
            unit_price NUMERIC(12,2) NOT NULL CHECK (unit_price >= 0));
outbox(id SERIAL PK, order_id INT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
       event_type TEXT NOT NULL,               -- order.created | order.status_updated
       payload JSONB NOT NULL,
       status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','failed')),
       attempts INT NOT NULL DEFAULT 0,
       next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
       last_error TEXT,
       created_at TIMESTAMPTZ NOT NULL DEFAULT now());
sync_state(key TEXT PK, value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
-- indexes: orders(user_id), orders(status), orders(order_date), orders(updated_at),
--          order_items(order_id), outbox(status, next_attempt_at)
```

App creates tables on startup if missing (SQLAlchemy metadata.create_all) — plus `scripts/schema.sql`
committed for reviewers. Money: Python `Decimal` end-to-end.

## 6. MongoDB product document

```json
{"_id": ObjectId, "sku": "WM-001", "title": "Wireless Mouse", "description": "...",
 "price": 50.16, "category": "peripherals", "tags": ["wireless","usb","office"],
 "attributes": {"color": "black", "dpi": 1600, "battery": "AA"},
 "variants": [{"sku":"WM-001-BLK","color":"black","stock":40},{"sku":"WM-001-WHT","color":"white","stock":12}],
 "active": true, "updated_at": ISODate}
```
Unique index on `sku`; index on `category`; index on `active`.

## 7. Elasticsearch mapping — index `orders`

- `order_id`: long
- `order_date`: date
- `status`: keyword
- `total_amount`: double
- `updated_at`: date
- `customer`: object → `id`: long, `name`: text + `keyword` subfield, `email`: keyword
- `items`: **nested** → `product_id`: keyword, `title`: text + `keyword` subfield,
  `quantity`: integer, `unit_price`: double
- Deterministic doc `_id` = `order-<order_id>` (idempotent upserts).

ES doc example per PDF. Reindex script builds docs from PG (users ⨝ orders ⨝ order_items) —
never from current Mongo catalog (snapshots only).

## 8. Synchronization strategies

Config: `SYNC_STRATEGY=dual_write|outbox|polling` (default `dual_write`).
All strategies use doc id `order-<id>` → retries are idempotent upserts.

1. **dual_write**: after PG commit, `es.index()` immediately.
   ES down/fails → catch, insert `outbox` row (status=pending) in a *separate* PG txn,
   return order with `es_sync.status="pending_retry"`. A lightweight repair loop
   (always on, every 10s) drains pending outbox rows → ES. PG truth never pretends rollback.
2. **outbox**: INSERT outbox row *inside the same PG txn* as order+items (atomic).
   Background worker polls `outbox WHERE status='pending' AND next_attempt_at <= now()`
   every `OUTBOX_POLL_SECONDS`, indexes to ES, marks done. Failures → attempts++,
   `next_attempt_at` = now + 2^attempts seconds, status=failed after 10 attempts (visible in
   /api/admin/sync-status). Latency ~ poll interval; survives ES outages and app restarts.
3. **polling**: worker every `POLLING_INTERVAL_SECONDS` (15s) selects
   `orders WHERE updated_at > watermark ORDER BY updated_at`, upserts each to ES,
   stores watermark in `sync_state`. Survives ES outage (watermark doesn't advance);
   risk: misses deletes (none in this POC) and high-churn edge cases — documented in NOTES.md.

Status updates (PATCH) flow through the same per-strategy path (ES update/upsert).
`scripts/reindex_orders.py` = full rebuild (admin-triggered or one-command after experiments).

## 9. Seed requirements (deterministic, seed=42)

- 8 users — exact names/emails from PDF §1.
- Products: 24 active (≥5 per category: peripherals/audio/cables/office; price bands
  ≥4 cheap <25, ≥4 mid 25–99, ≥4 premium ≥100; ≥6 with "wireless" in title/tags) + 1 inactive.
  Required titles/prices: Wireless Mouse 50.16, Mechanical Keyboard 100.34, Wireless Earbuds 79.99,
  USB-C Hub 7-in-1 45.00, Desk Lamp LED 32.50, Noise Cancelling Headphones 199.00.
- Orders: 44 orders, ≥80 items. Status: 15 PENDING / 15 PROCESSING / 14 SHIPPED (≥10 each).
  Dates: spread over last 90 days; ≥5 older than 60 days (Morgan Lee / Riley Brooks);
  ≥5 within last 7 days. Totals: ≥5 under $30, ≥5 $30–$150, ≥5 over $200.
  Per-user: every user ≥2 orders. Wendy Wireless ≥3 orders containing wireless-tagged products.
  Fixtures: ≥8 orders contain "Wireless Mouse" item, ≥3 contain "Mechanical Keyboard",
  ≥2 contain both. Every total == Σ qty×unit_price.
- After orders: rename Mongo "Wireless Mouse" → "Wireless Mouse Pro" + new price
  (e.g. 59.99). Do NOT touch PG/ES history.
- Finally: full reindex → ES doc count == PG order count. Seed is idempotent-ish:
  `seed.py --reset` drops/recreates (documented); default run skips existing users/products.

## 10. Frontend screens & routes

Design tokens: warm white `#FAFAF9` bg, white surfaces, `stone-200` borders, `stone-900` text,
accent `indigo-600` (hover `indigo-700`), badges: PENDING amber-100/amber-800, PROCESSING blue-100/blue-800,
SHIPPED emerald-100/emerald-800. Rounded-xl cards, subtle shadows, Inter-ish system font stack.
Lucide icons. Responsive (grid → 1 col mobile), skeletons, empty states, toasts, form validation.

- `/` Storefront: header (logo, "Log In As" select from GET /api/users, cart button w/ count),
  search input + category filter chips, product grid (image placeholder w/ icon, title, desc,
  price, category badge, tags, Add to Cart + qty stepper), cart drawer (lines, qty +/-, remove,
  subtotal, "Proceed to Checkout"). Banner: "Demo POC — no real authentication".
- `/checkout`: order summary table (title, qty, unit price, line total), customer card,
  Place Order button (loading state), success → order ID + link to admin order page; failure toast.
  Sends idempotency_key (crypto.randomUUID) per submission; disables double-click.
- `/admin` Dashboard: sidebar (Dashboard, Orders→same page, Catalog, Storefront link),
  KPI cards (Revenue, Total orders, Pending/Processing/Shipped counts),
  omni-search input, filter panel (status checkboxes, date from/to, min/max total, sort),
  orders table (ID link, date, customer, total, status badge), pagination, loading skeletons.
  Uses POST /api/search/orders ONLY.
- `/admin/orders/:id` Order details: meta, customer, status dropdown + Save, receipt table
  (title snapshot, qty, unit price, line total), sync badge
  (In sync / Pending sync / ES unreachable via GET /api/orders/{id}.sync).
- `/admin/catalog` Catalog admin: table/grid of products (all, incl. inactive w/ badge),
  category + active filters, Create/Edit modal form (title, sku, description, price, category,
  tags comma-separated, attributes key=value editor, variants editor rows, active toggle),
  validation + toasts. Writes Mongo only.

## 11. Testing

pytest: unit tests mock Mongo/ES (and PG via sqlite? No — PG-specific SQL; use mocks/fakes).
Integration tests marked `integration`, skipped when services unreachable.
Cover: product filtering + inactive hidden; product create/update; checkout price validation
(server prices win), totals, atomic rollback (force item insert failure → no order row),
idempotent duplicate; order read from PG; status update → ES doc updated; search q across
customer name + item title; status/date/price filters; aggs revenue correctness vs filters;
pagination; snapshot preservation after catalog edit; ES-down checkout still commits PG.

## 12. Docs

README.md: prereqs, env, docker compose up, venv+pip install, schema, seed, run uvicorn,
run Vite, URLs (frontend :5173, API :8000, docs :8000/docs), tests, reindex, 5-screen demo,
both sync strategies demo, troubleshooting.
NOTES.md: answers to the 5 Learning Milestones (split-brain, why-not-SQL-search,
why-Mongo-catalog, snapshotting, write-path discipline).

## 13. Conventions

- UTC-aware datetimes everywhere; serialize ISO-8601.
- Money: Decimal server-side, NUMERIC(12,2) PG, round-half-up to 2dp.
- Structured logging (no credentials), error envelope `{"detail": ...}`, no stack traces to clients.
- CORS: allow Vite origins (http://localhost:5173, http://127.0.0.1:5173).
- `.env.example` committed; never commit `.env`.
- Health/timeout handling on all DB clients (timeouts 5s, graceful degraded responses).
