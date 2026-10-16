# Byte 5: Product title and price snapshots

**Builds on:** 3, 4

**In plain terms:**
An order item stores the product title and price that applied at checkout.
These copied values are called a snapshot.

**The code:**
```python
# Representative pattern; verify against the repository.
order_item = {
    "product_id": product["id"],
    "product_title": product["title"],
    "unit_price": product["price"],
    "quantity": quantity,
}
```

**What's happening:**
The checkout flow reads the current product document and copies the required values into the order item.
A later catalog edit changes the product document but does not rewrite the old order item.

**Why it matters:**
Historical orders must show what the customer bought and paid at the time.
A live catalog lookup alone could show a new title or price for an old purchase.
