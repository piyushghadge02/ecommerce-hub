# Byte 11: Acceptance demo flow

**Builds on:** 2, 3, 4, 5, 6, 7, 8, 9

**In plain terms:**
The demo proves that the storefront, checkout, search dashboard, order details, and catalog admin work together.
Run the steps in order so the evaluator can see the effect of each change.

**The code:**
```text
1. Browse active products.
2. Add a product to the cart and place an order.
3. Search for Wireless in the admin dashboard.
4. Apply status, date, and price filters; inspect KPIs.
5. Open the order and change its status.
6. Rename Wireless Mouse to Wireless Mouse Pro.
7. Confirm the old order still shows Wireless Mouse.
```

**What's happening:**
The sequence creates a new order before the catalog edit.
It then tests search, filtering, status synchronization, and historical snapshots.

**Why it matters:**
This sequence directly checks the main data boundaries.
It also avoids a false result caused by renaming the product before checkout.
