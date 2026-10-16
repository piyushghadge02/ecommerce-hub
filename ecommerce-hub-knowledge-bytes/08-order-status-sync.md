# Byte 8: Order status updates and index synchronization

**Builds on:** 4, 6, 7

**In plain terms:**
An order status update must first change the authoritative PostgreSQL record.
The Elasticsearch document must then receive the new status.

**The code:**
```python
# Representative pattern; verify against the repository.
await update_order_status_in_postgres(order_id, new_status)
await sync_order_document_to_elasticsearch(order_id)
```

**What's happening:**
The first operation changes the source record.
The second operation updates the search projection so the admin dashboard can show the new status.

**Why it matters:**
A direct dual write can fail between the two operations.
The implementation must define how it reports and repairs a PostgreSQL/Elasticsearch mismatch.
Check the repository to identify the actual synchronization strategies used.
