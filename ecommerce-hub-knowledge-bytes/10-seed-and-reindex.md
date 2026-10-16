# Byte 10: Seed data and Elasticsearch reindex

**Builds on:** 3, 4, 5, 6, 7

**In plain terms:**
Seed scripts create repeatable sample users, products, orders, and order items.
A reindex command rebuilds Elasticsearch documents from PostgreSQL order data.

**The code:**
```bash
# Representative command names from the assignment; verify actual scripts.
python -m scripts.seed
python -m scripts.reindex
```

**What's happening:**
The seed operation prepares the sample data required for the demo.
The reindex operation reads authoritative order data and recreates the search index documents.

**Why it matters:**
A reviewer can test the same scenarios after a clean setup.
Reindexing provides a recovery path after an index loss or a synchronization experiment.
The actual script names and options must match the repository.
