# Byte 2: Vue frontend and FastAPI API

**Builds on:** 1

**In plain terms:**
The Vue frontend presents the storefront, checkout, order search, order details, and catalog administration.
FastAPI exposes HTTP endpoints that the frontend calls.

**The code:**
```javascript
// Representative pattern; verify against the repository.
const apiBase = import.meta.env.VITE_API_URL || "";

export async function getProducts() {
  const response = await fetch(`${apiBase}/api/products?active=true`);
  if (!response.ok) throw new Error("Product request failed");
  return response.json();
}
```

**What's happening:**
The frontend calls an API route and checks the HTTP response.
An empty base URL can allow a Vite proxy or same-origin reverse proxy to route `/api` requests to FastAPI.

**Why it matters:**
A shared API path can work in local development and behind a reverse proxy.
The frontend must show an error when the API request fails instead of presenting false data.
