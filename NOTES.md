# Learning Milestones — NOTES.md

## 1. Split-brain: Elasticsearch down for 5 minutes while customers keep ordering

**What happens:** checkout (`POST /api/orders`) never depends on Elasticsearch to
succeed. The PostgreSQL transaction — order row + order-item rows (+ outbox row in
`outbox` mode) — commits first and independently. Elasticsearch is contacted only
*after* the commit, so an ES outage cannot fail or roll back a paid order.

**Recovery per strategy:**

- **dual_write** (default): the immediate `es.index()` raises → we catch it, insert an
  `outbox` row (`status='pending'`) in a separate short transaction, and return
  `201` with `es_sync.status = "pending_retry"` — honest about what happened, never
  pretending the PG transaction rolled back. A repair loop (every 10s) drains pending
  outbox rows into ES. Verified live: with ES killed, an order committed in PG with
  `pending_retry`; after ES restarted, the repair loop indexed it (outbox row went
  `pending → done` after 4 attempts) and the admin dashboard caught up.
- **outbox**: nothing changes during the outage — the worker keeps polling, attempts
  fail with exponential backoff (`next_attempt_at = now + 2^attempts`), and rows go
  `failed` only after 10 attempts, where `GET /api/admin/sync-status` makes the
  backlog visible. When ES returns, the worker drains the queue in order.
- **polling**: the watermark in `sync_state` simply doesn't advance while ES is down;
  on recovery the next 15s cycle picks up every order changed since the last
  successful poll. No data loss, at most one poll interval of staleness.

**During the outage** the admin dashboard (`/admin`) shows stale results — it reads
ES only, by design. Order Details (`/admin/orders/:id`) still shows live PostgreSQL
data, and its sync badge reports "ES unreachable" instead of silently disagreeing.

## 2. Why not SQL search? (Inverted indexes vs B-trees)

Screen 3 needs: partial-text matching across customer names, emails, and *every*
line-item title in an order ("wireless" → "Wireless Mouse" *and* "Wendy Wireless"),
combined with status/date/price filters, plus revenue and per-status aggregations
over exactly the filtered set.

In PostgreSQL that becomes a multi-table JOIN (`users ⨝ orders ⨝ order_items`) with
`WHERE ... LIKE '%wireless%'`. A B-tree index cannot serve a leading-wildcard LIKE —
the pattern can start anywhere, so the planner falls back to a sequential scan over
the joined rows, and the JOIN itself multiplies rows before filtering. Add stemming,
relevance ranking, or typo tolerance and you're hand-rolling a search engine in SQL.

An **inverted index** (what Elasticsearch/Lucene builds) maps each *term* → the list
of documents containing it. "wireless" is one dictionary lookup, returning exactly
the matching order documents — no JOINs (the order is already denormalized into one
document), no scans. Filters become bitset intersections, and aggregations
(`sum(total_amount)`, `terms(status)`) run over the same filtered bitset in one pass,
so the KPI cards can never disagree with the result table. B-trees are optimal for
"find row by key / range over one column"; inverted indexes are optimal for
"find documents containing these terms, then facet over them."

## 3. Why MongoDB for the catalog?

Product data is *heterogeneous and evolving*: a mouse has `dpi` and `battery`, a
cable has `port types` and `length`, a keyboard has `switch type` — and next quarter
someone invents a new attribute. In normalized PostgreSQL every new attribute is a
schema migration (or an EAV anti-pattern, or a `JSONB` column that abandons the
relational model anyway), and variants (color/size/SKU/stock per variant) need a
separate table plus JOINs for the most common read: "show me the product."

A MongoDB document stores the product exactly as the storefront renders it:
`tags[]`, an open `attributes{}` map, and a `variants[]` array with per-variant SKU
and stock — one read, no JOINs, no migrations when marketing adds `energy_rating`.
The catalog is also read-heavy and rarely joined to anything (orders snapshot what
they need at checkout), so the document model's weak spot — cross-document
consistency — never bites here.

## 4. Snapshotting: why copy title and price into order items and ES documents?

Because the catalog is *mutable* and history must be *immutable*. The seed
demonstrates it deliberately: "Wireless Mouse" ($50.16) is renamed to
"Wireless Mouse Pro" ($59.99) in MongoDB after orders exist. If order screens
joined the live catalog, every historical receipt, the revenue KPIs, and past
search results would silently rewrite themselves — a customer support nightmare
("I was charged $59.99 for something I bought at $50.16") and an accounting one.

So at checkout the server copies `product_id`, `title`, `unit_price`, `quantity`
into `order_items` inside the same PostgreSQL transaction, and the Elasticsearch
document carries the same snapshots. Catalog edits (`PUT /api/products`) touch
MongoDB only — verified: 0 ES documents contain "Wireless Mouse Pro" while the
storefront shows the new name. Only *new* checkouts pick up new titles/prices.

## 5. Write-path discipline: which screens may write to which store?

| Screen | May write to | Must never write to |
|---|---|---|
| Storefront | — (cart is local state) | any database |
| Checkout | PostgreSQL (orders + items, one transaction) | MongoDB, Elasticsearch directly (ES only via the sync strategies) |
| Search dashboard | — (read-only) | any database |
| Order details | PostgreSQL (status), then ES via sync | MongoDB |
| Catalog admin | MongoDB (products) | PostgreSQL, Elasticsearch |

**What goes wrong if orders are also written to MongoDB "for convenience"?**
You create a second source of truth with no transaction spanning both stores, so
every write becomes a distributed-consistency problem you now own: partial failures
(order in PG but not Mongo), divergent status values, and sync code in both
directions. Reads then have to pick a winner per screen, and the "which store is
authoritative?" question — the entire point of polyglot persistence — has no answer.
The discipline is simple: money and fulfillment live in exactly one place
(PostgreSQL); the catalog lives in exactly one place (MongoDB); Elasticsearch is a
derived, rebuildable index (`reindex_orders.py` can reconstruct it fully from PG).
