-- PostgreSQL schema for the E-Commerce Order Management & Search Service.
-- Also created automatically at app startup via SQLAlchemy metadata.create_all.
-- Money: NUMERIC(12,2). Timestamps: TIMESTAMPTZ (UTC). Statuses: exact
-- uppercase strings shared by the API and Elasticsearch.

CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS orders (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    order_date      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CONSTRAINT ck_orders_status CHECK (status IN ('PENDING','PROCESSING','SHIPPED')),
    total_amount    NUMERIC(12,2) NOT NULL
                    CONSTRAINT ck_orders_total_nonneg CHECK (total_amount >= 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_orders_user_id    ON orders(user_id);
CREATE INDEX IF NOT EXISTS ix_orders_status      ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_order_date  ON orders(order_date);
CREATE INDEX IF NOT EXISTS ix_orders_updated_at  ON orders(updated_at);

CREATE TABLE IF NOT EXISTS order_items (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id  TEXT NOT NULL,          -- MongoDB ObjectId as text
    title       TEXT NOT NULL,          -- snapshot taken at checkout
    quantity    INTEGER NOT NULL CONSTRAINT ck_order_items_qty CHECK (quantity >= 1),
    unit_price  NUMERIC(12,2) NOT NULL
                CONSTRAINT ck_order_items_price_nonneg CHECK (unit_price >= 0)
);
CREATE INDEX IF NOT EXISTS ix_order_items_order_id ON order_items(order_id);

-- Transactional outbox for the outbox / dual-write-repair sync strategies.
CREATE TABLE IF NOT EXISTS outbox (
    id              SERIAL PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,      -- order.created | order.status_updated
    payload         JSONB NOT NULL,     -- full Elasticsearch document snapshot
    status          TEXT NOT NULL DEFAULT 'pending'
                    CONSTRAINT ck_outbox_status CHECK (status IN ('pending','processing','done','failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_outbox_status_next_attempt ON outbox(status, next_attempt_at);

-- Named watermarks (e.g. polling_watermark) for the polling sync strategy.
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
