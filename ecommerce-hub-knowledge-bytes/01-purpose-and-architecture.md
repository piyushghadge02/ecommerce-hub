# Byte 1: E-Commerce Hub purpose and architecture

**Builds on:** None (first byte)

**In plain terms:**
E-Commerce Hub is an e-commerce application with a storefront and an admin order dashboard.
It uses three data stores because each store has a different responsibility.

**The code:**
```text
Vue 3 storefront and admin pages
             |
          FastAPI
        /    |     \
 PostgreSQL MongoDB Elasticsearch
 orders     catalog  order search
```

**What's happening:**
Vue 3 displays pages and sends HTTP requests to FastAPI.
FastAPI applies application rules and communicates with the data stores.
PostgreSQL owns order and user records, MongoDB owns catalog documents, and Elasticsearch supports admin search.

**Why it matters:**
Each store has one clear role.
This reduces the risk of using a search index as the source of truth for an order.
