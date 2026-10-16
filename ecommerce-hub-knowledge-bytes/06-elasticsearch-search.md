# Byte 6: Elasticsearch order search

**Builds on:** 1, 4, 5

**In plain terms:**
Elasticsearch stores searchable copies of order data.
An index is a collection of documents that Elasticsearch can search.

**The code:**
```python
# Representative pattern; verify against the repository.
document = {
    "order_id": order.id,
    "customer": {"name": customer.name},
    "items": order_item_snapshots,
    "status": order.status,
    "total_amount": order.total_amount,
    "order_date": order.order_date,
    "updated_at": order.updated_at,
}
```

**What's happening:**
The indexing code maps PostgreSQL order data to a search document.
The document includes customer name, nested item details, status, total amount, and dates when those fields are part of the mapping.

**Why it matters:**
The admin dashboard can search order and product text without running a large relational text query for every request.
Elasticsearch is a search projection, not the authoritative order database.
