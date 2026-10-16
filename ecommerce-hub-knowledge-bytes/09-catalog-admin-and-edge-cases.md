# Byte 9: Catalog administration and edge cases

**Builds on:** 3, 5, 8

**In plain terms:**
Catalog administration changes product documents in MongoDB.
A catalog edit must not change the snapshot data stored in an existing order.

**The code:**
```python
# Representative pattern; verify against the repository.
await db.products.update_one(
    {"sku": sku},
    {"$set": {"title": new_title}}
)
```

**What's happening:**
The update changes the current product title.
Previously created order items retain their saved title and unit price because they are separate order records.

**Why it matters:**
This separation preserves purchase history.
The application must also handle a missing SKU, invalid price, inactive product, or unavailable data store with a clear error.
