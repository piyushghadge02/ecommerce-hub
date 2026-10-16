# E-Commerce Hub — Knowledge Bytes

## Purpose
This folder divides the E-Commerce Hub project into short, independent knowledge bytes. The structure follows the attached **Knowledge Bytes Prompt (Model-Agnostic, STE100)** PDF.

## Project context
E-Commerce Hub is a Python FastAPI + Vue 3/Vite application. It uses:
- PostgreSQL as the authoritative store for users and orders.
- MongoDB for the product catalog.
- Elasticsearch for administrative order search, facets, and aggregations.
- Docker Compose to run the data services.

## Important source note
The attached PDF contains the knowledge-byte writing prompt, not the E-Commerce Hub source code. The project source directory or a ZIP of the source code was not attached to this request. Therefore:
- These bytes are based on the assignment requirements and implementation details shared in the conversation.
- Code blocks are representative patterns, not verified verbatim extracts from the repository.
- Before using these as source-code documentation for evaluation, compare each code block and file path with the current repository and replace representative snippets with exact source snippets.

## Contents
1. `01-purpose-and-architecture.md` — purpose and service boundaries
2. `02-frontend-and-api.md` — Vue storefront and API flow
3. `03-product-catalog-mongodb.md` — product catalog and active-product filtering
4. `04-checkout-and-postgresql.md` — transactional order creation
5. `05-order-item-snapshots.md` — historical title and price snapshots
6. `06-elasticsearch-search.md` — order indexing and omni-search
7. `07-filters-and-kpis.md` — facets, date/price filters, and aggregations
8. `08-order-status-sync.md` — status updates and search-index synchronization
9. `09-catalog-admin-and-edge-cases.md` — catalog edits and failure cases
10. `10-seed-and-reindex.md` — seed data and index rebuild
11. `11-demo-flow.md` — concise acceptance demo
12. `12-learning-milestones.md` — split-brain, SQL search, MongoDB, snapshots, and write discipline

## Reading order
Read bytes 1–3 first for context and structure. Then read bytes 4–8 for the order flow and search. Read bytes 9–12 for edge cases, seed/reindex, demo, and design trade-offs.

## Knowledge-byte format
Each byte includes:
- Builds on
- In plain terms
- The code (representative pattern)
- What's happening
- Why it matters

The prose aims to follow the attached PDF's ASD-STE100 Simplified Technical English rules. Code identifiers and code blocks are exempt from those language rules.
