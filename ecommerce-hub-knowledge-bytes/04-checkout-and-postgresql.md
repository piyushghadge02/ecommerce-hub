# Byte 4: Transactional order creation

**Builds on:** 1, 2, 3

**In plain terms:**
Checkout creates an order and its order items in PostgreSQL.
A database transaction groups these writes into one unit of work.

**The code:**
```python
# Representative pattern; verify against the repository.
async with db.transaction():
    order_id = await insert_order(customer_id, total_amount)
    await insert_order_items(order_id, validated_items)
```

**What's happening:**
The application validates the checkout request before it writes the order.
The transaction commits the order and its items together, or rolls back the transaction if a database operation fails.

**Why it matters:**
The database must not contain an order without its required items.
PostgreSQL remains authoritative even when Elasticsearch is delayed or unavailable.
