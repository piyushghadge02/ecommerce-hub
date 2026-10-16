# Byte 3: Product catalog in MongoDB

**Builds on:** 1, 2

**In plain terms:**
MongoDB stores products as documents.
A product document can contain a title, price, category, SKU, active flag, and image information.

**The code:**
```python
# Representative pattern; verify against the repository.
products = await db.products.find({"active": True}).to_list(length=200)
```

**What's happening:**
The query selects only products marked active.
The API returns those products to the storefront, where Vue renders the product grid.

**Why it matters:**
The storefront must not show inactive products.
A document model can also support product fields that vary between categories.
