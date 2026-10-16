"""Products API tests: unit (fake Mongo) + integration (real MongoDB)."""
from __future__ import annotations

from .conftest import call, make_app, make_itest_app


# ------------------------------------------------------------ unit (fake) ---
def test_list_hides_inactive_by_default(seeded_mongo):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db)
    status, data = call(app, "GET", "/api/products")
    assert status == 200
    titles = [p["title"] for p in data]
    assert "Wireless Mouse" in titles and "Mechanical Keyboard" in titles
    assert "Old CRT" not in titles


def test_list_active_all_and_false(seeded_mongo):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db)
    status, data = call(app, "GET", "/api/products", params={"active": "all"})
    assert status == 200 and len(data) == 3
    status, data = call(app, "GET", "/api/products", params={"active": "false"})
    assert [p["title"] for p in data] == ["Old CRT"]


def test_list_category_and_q_filters(seeded_mongo):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db)
    _, data = call(app, "GET", "/api/products", params={"category": "peripherals"})
    assert len(data) == 2
    _, data = call(app, "GET", "/api/products", params={"category": "office"})
    assert len(data) == 0  # inactive hidden
    _, data = call(app, "GET", "/api/products",
                   params={"category": "office", "active": "all"})
    assert len(data) == 1
    _, data = call(app, "GET", "/api/products", params={"q": "wireless"})
    assert [p["title"] for p in data] == ["Wireless Mouse"]
    _, data = call(app, "GET", "/api/products", params={"q": "KEYBOARD"})
    assert [p["title"] for p in data] == ["Mechanical Keyboard"]
    _, data = call(app, "GET", "/api/products", params={"q": "usb"})  # tags match
    assert [p["title"] for p in data] == ["Wireless Mouse"]


def test_get_product_and_404s(seeded_mongo):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db)
    status, data = call(app, "GET", f"/api/products/{ids['WM-001']}")
    assert status == 200
    assert data["sku"] == "WM-001"
    assert data["variants"][0]["sku"] == "WM-001-BLK"
    status, _ = call(app, "GET", "/api/products/not-a-hex-id")
    assert status == 404
    status, data = call(app, "GET", "/api/products/000000000000000000000000")
    assert status == 404
    assert "detail" in data


def test_create_product_validation(seeded_mongo):
    db, ids = seeded_mongo
    app, _ = make_app(fake_mongo=db)
    base = {
        "sku": "NEW-1", "title": "New Gadget", "description": "d",
        "price": 9.99, "category": "office", "tags": ["new"],
        "attributes": {"color": "red"},
        "variants": [{"sku": "NEW-1-R", "color": "red", "stock": 3}],
        "active": True,
    }
    status, data = call(app, "POST", "/api/products", json=base)
    assert status == 201
    assert data["id"]
    status, _ = call(app, "POST", "/api/products", json=base)  # duplicate sku
    assert status == 409
    bad = dict(base, sku="NEW-2", title="")
    assert call(app, "POST", "/api/products", json=bad)[0] == 422
    bad = dict(base, sku="NEW-3", price=0)
    assert call(app, "POST", "/api/products", json=bad)[0] == 422
    bad = dict(base, sku="NEW-4", variants=[])
    assert call(app, "POST", "/api/products", json=bad)[0] == 422


def test_update_product_mongo_only(seeded_mongo, fake_es):
    db, ids = seeded_mongo
    # wire the ES fake too, to prove catalog writes never touch ES
    app, _ = make_app(fake_mongo=db, fake_es=fake_es)
    status, data = call(app, "PUT", f"/api/products/{ids['WM-001']}",
                        json={"title": "Wireless Mouse Pro", "price": 59.99})
    assert status == 200
    assert data["title"] == "Wireless Mouse Pro"
    assert data["price"] == 59.99
    assert data["sku"] == "WM-001"  # untouched fields survive partial update
    status, _ = call(app, "PUT", "/api/products/000000000000000000000000",
                     json={"title": "x"})
    assert status == 404
    status, _ = call(app, "PUT", f"/api/products/{ids['WM-001']}", json={})
    assert status == 422
    assert fake_es.index_calls == 0  # no ES writes from catalog admin


# ------------------------------------------------- integration (real mongo) ---
def test_integration_product_crud(itest_settings, real_mongo_db, real_es_client,
                                  itest_mongo_products):
    app, _ = make_itest_app(real_mongo_db, real_es_client, itest_settings)
    _, data = call(app, "GET", "/api/products")
    titles = [p["title"] for p in data]
    assert "Wireless Mouse" in titles and "Old CRT" not in titles

    payload = {
        "sku": "IT-1", "title": "Integration Widget", "price": 12.5,
        "category": "office", "tags": ["itest"],
        "attributes": {"size": "M"},
        "variants": [{"sku": "IT-1-M", "stock": 7}], "active": True,
    }
    status, created = call(app, "POST", "/api/products", json=payload)
    assert status == 201
    status, fetched = call(app, "GET", f"/api/products/{created['id']}")
    assert fetched["attributes"] == {"size": "M"}
    status, updated = call(app, "PUT", f"/api/products/{created['id']}",
                           json={"price": 15.0, "active": False})
    assert updated["price"] == 15.0 and updated["active"] is False
    _, data = call(app, "GET", "/api/products")
    assert "Integration Widget" not in [p["title"] for p in data]
    _, data = call(app, "GET", "/api/products", params={"active": "all"})
    assert "Integration Widget" in [p["title"] for p in data]
