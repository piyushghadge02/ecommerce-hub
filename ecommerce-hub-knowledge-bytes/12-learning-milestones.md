# Byte 12: Architecture trade-offs and learning milestones

**Builds on:** 1, 3, 4, 5, 6, 7, 8, 10

**In plain terms:**
E-Commerce Hub separates transactional writes, catalog documents, and search reads.
This separation improves fit for each workload but introduces synchronization work.

**The code:**
```text
PostgreSQL = authoritative users and orders
MongoDB = current product catalog
Elasticsearch = searchable order projection
```

**What's happening:**
PostgreSQL protects order consistency.
MongoDB stores product documents with flexible fields.
Elasticsearch supports full-text search, facets, and aggregations, but it can temporarily lag behind PostgreSQL.

**Why it matters:**
- Split-brain: if Elasticsearch is unavailable, order writes should still use PostgreSQL.
- SQL search: large joins plus wildcard text matching can be costly for admin search.
- Snapshots: saved title and price protect order history.
- Write discipline: PostgreSQL owns order truth; MongoDB must not become a second order store.
- Recovery: a seed or reindex command should restore search documents from authoritative data.

**PUTTING IT TOGETHER**
The Vue frontend sends user actions to FastAPI. FastAPI reads current products from MongoDB and writes orders to PostgreSQL. Checkout stores product title and price snapshots in each order item. Elasticsearch receives an order-shaped document for admin search, filters, and KPI aggregations. Status updates must synchronize the PostgreSQL record and the search document. Seed and reindex scripts support repeatable demos and recovery.
