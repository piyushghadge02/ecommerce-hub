# Byte 7: Search filters, facets, and KPI aggregations

**Builds on:** 6

**In plain terms:**
A filter restricts the orders that match a search.
A facet reports counts for a field, and an aggregation calculates summary values such as revenue.

**The code:**
```python
# Representative Elasticsearch query shape; verify against the repository.
query = {
    "query": {"bool": {"filter": [
        {"terms": {"status": selected_statuses}},
        {"range": {"total_amount": {"gte": min_price, "lte": max_price}}},
    ]}},
    "aggs": {
        "revenue": {"sum": {"field": "total_amount"}},
        "by_status": {"terms": {"field": "status"}},
    },
}
```

**What's happening:**
The query combines structured filters with the search criteria.
Aggregations calculate total amount and counts grouped by status for the matching set.

**Why it matters:**
Administrators can narrow results by status, date, and price.
KPI values help them understand the filtered order set without opening each order.
