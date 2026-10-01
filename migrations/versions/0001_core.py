"""Core transactional schema: tenants, catalog, inventory, recommendations, orders,
payments, notifications, approvals, idempotency, audit, conversations, LLM usage.

Design notes
------------
* Every tenant-owned table carries ``tenant_id`` and a ``UNIQUE (tenant_id, <pk>)`` so child
  tables can reference parents with **composite foreign keys** that include the tenant. A
  row can therefore never point at another tenant's parent row, even via a bug.
* Money is ``NUMERIC(14,2)`` (never float); quantities are integers with CHECK constraints.
* Status columns use CHECK constraints (cheaper to evolve than PG enums).
* ``idempotency_key`` is unique per tenant on every mutation-created table.

Revision ID: 0001_core
Revises:
Create Date: 2026-09-30
"""

from alembic import op

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

CREATE TABLE tenants (
    tenant_id            TEXT PRIMARY KEY CHECK (tenant_id ~ '^[a-z0-9-]{3,64}$'),
    name                 TEXT NOT NULL,
    currency             CHAR(3) NOT NULL DEFAULT 'USD',
    approval_threshold   NUMERIC(14,2) NOT NULL DEFAULT 10000 CHECK (approval_threshold >= 0),
    payment_limit        NUMERIC(14,2) NOT NULL DEFAULT 50000 CHECK (payment_limit >= 0),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    tenant_id     TEXT NOT NULL REFERENCES tenants(tenant_id),
    user_id       TEXT NOT NULL,
    email         TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    roles         TEXT[] NOT NULL DEFAULT '{viewer}',
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    CHECK (roles <@ ARRAY['viewer','buyer','approver','admin']::TEXT[])
);

CREATE TABLE vendors (
    vendor_id       UUID PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id),
    vendor_code     TEXT NOT NULL,
    name            TEXT NOT NULL,
    lead_time_days  INTEGER NOT NULL CHECK (lead_time_days BETWEEN 0 AND 180),
    min_order_qty   INTEGER NOT NULL DEFAULT 1 CHECK (min_order_qty > 0),
    payment_terms   TEXT NOT NULL DEFAULT 'NET30',
    rating          NUMERIC(3,2) NOT NULL DEFAULT 4.00 CHECK (rating BETWEEN 0 AND 5),
    contact_email   TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, vendor_code),
    UNIQUE (tenant_id, vendor_id)
);

CREATE TABLE products (
    product_id         UUID PRIMARY KEY,
    tenant_id          TEXT NOT NULL REFERENCES tenants(tenant_id),
    sku                TEXT NOT NULL CHECK (sku ~ '^[A-Z0-9-]{3,32}$'),
    name               TEXT NOT NULL,
    category           TEXT NOT NULL,
    uom                TEXT NOT NULL DEFAULT 'EA',
    unit_cost          NUMERIC(12,2) NOT NULL CHECK (unit_cost >= 0),
    default_vendor_id  UUID NOT NULL,
    is_active          BOOLEAN NOT NULL DEFAULT true,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, sku),
    UNIQUE (tenant_id, product_id),
    FOREIGN KEY (tenant_id, default_vendor_id) REFERENCES vendors(tenant_id, vendor_id)
);

CREATE TABLE locations (
    location_id    UUID PRIMARY KEY,
    tenant_id      TEXT NOT NULL REFERENCES tenants(tenant_id),
    location_code  TEXT NOT NULL,
    name           TEXT NOT NULL,
    location_type  TEXT NOT NULL CHECK (location_type IN ('store','dc')),
    region         TEXT NOT NULL,
    city           TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, location_code),
    UNIQUE (tenant_id, location_id)
);

CREATE TABLE inventory_positions (
    tenant_id         TEXT NOT NULL,
    product_id        UUID NOT NULL,
    location_id       UUID NOT NULL,
    on_hand           INTEGER NOT NULL CHECK (on_hand >= 0),
    on_order          INTEGER NOT NULL DEFAULT 0 CHECK (on_order >= 0),
    allocated         INTEGER NOT NULL DEFAULT 0 CHECK (allocated >= 0),
    safety_stock      INTEGER NOT NULL CHECK (safety_stock >= 0),
    avg_daily_demand  NUMERIC(10,2) NOT NULL CHECK (avg_daily_demand >= 0),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, product_id, location_id),
    FOREIGN KEY (tenant_id, product_id) REFERENCES products(tenant_id, product_id),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, location_id)
);

CREATE TABLE recommendations (
    recommendation_id       UUID PRIMARY KEY,
    tenant_id               TEXT NOT NULL REFERENCES tenants(tenant_id),
    product_id              UUID NOT NULL,
    sku                     TEXT NOT NULL,
    location_id             UUID NOT NULL,
    vendor_id               UUID NOT NULL,
    current_stock           INTEGER NOT NULL CHECK (current_stock >= 0),
    forecast_demand         NUMERIC(12,2) NOT NULL CHECK (forecast_demand >= 0),
    forecast_horizon_days   INTEGER NOT NULL DEFAULT 14 CHECK (forecast_horizon_days BETWEEN 1 AND 120),
    safety_stock            INTEGER NOT NULL CHECK (safety_stock >= 0),
    recommended_qty         INTEGER NOT NULL CHECK (recommended_qty > 0),
    unit_cost               NUMERIC(12,2) NOT NULL CHECK (unit_cost >= 0),
    reason                  TEXT NOT NULL,
    risk_level              TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    expected_delivery_date  DATE NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open','accepted','ordered','dismissed','expired')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, recommendation_id),
    FOREIGN KEY (tenant_id, product_id) REFERENCES products(tenant_id, product_id),
    FOREIGN KEY (tenant_id, sku) REFERENCES products(tenant_id, sku),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, location_id),
    FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors(tenant_id, vendor_id)
);
CREATE INDEX ix_rec_tenant_status ON recommendations (tenant_id, status, risk_level);
CREATE INDEX ix_rec_tenant_loc_prod ON recommendations (tenant_id, location_id, product_id);
CREATE INDEX ix_rec_tenant_sku ON recommendations (tenant_id, sku);
-- at most one OPEN recommendation per product/location
CREATE UNIQUE INDEX ux_rec_open_per_item ON recommendations (tenant_id, product_id, location_id) WHERE status = 'open';

CREATE TABLE approvals (
    approval_id      UUID PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(tenant_id),
    conversation_id  UUID,
    requested_by     TEXT NOT NULL,
    action           TEXT NOT NULL CHECK (action IN ('create_order','process_payment','update_order','cancel_order')),
    payload          JSONB NOT NULL,
    payload_hash     CHAR(64) NOT NULL,
    summary          TEXT NOT NULL,
    required_role    TEXT NOT NULL DEFAULT 'buyer' CHECK (required_role IN ('buyer','approver','admin')),
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','approved','rejected','expired','consumed')),
    decided_by       TEXT,
    decided_at       TIMESTAMPTZ,
    decision_comment TEXT,
    expires_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, approval_id)
);
CREATE INDEX ix_approvals_conv ON approvals (tenant_id, conversation_id, status);

CREATE TABLE orders (
    order_id                UUID PRIMARY KEY,
    tenant_id               TEXT NOT NULL REFERENCES tenants(tenant_id),
    order_number            TEXT NOT NULL,
    recommendation_id       UUID,
    location_id             UUID NOT NULL,
    vendor_id               UUID NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
                            ('draft','pending_approval','approved','submitted','fulfilled','cancelled','payment_failed')),
    payment_status          TEXT NOT NULL DEFAULT 'not_started' CHECK (payment_status IN
                            ('not_started','pending','authorized','captured','failed','refunded')),
    currency                CHAR(3) NOT NULL DEFAULT 'USD',
    total_amount            NUMERIC(14,2) NOT NULL CHECK (total_amount >= 0),
    expected_delivery_date  DATE NOT NULL,
    idempotency_key         TEXT,
    approval_id             UUID,
    created_by              TEXT NOT NULL,
    notes                   TEXT,
    version                 INTEGER NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, order_number),
    UNIQUE (tenant_id, order_id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, recommendation_id) REFERENCES recommendations(tenant_id, recommendation_id),
    FOREIGN KEY (tenant_id, location_id) REFERENCES locations(tenant_id, location_id),
    FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors(tenant_id, vendor_id),
    FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals(tenant_id, approval_id),
    CHECK (NOT (status = 'cancelled' AND payment_status = 'captured'))
);
CREATE INDEX ix_orders_tenant_status ON orders (tenant_id, status, payment_status);
CREATE INDEX ix_orders_tenant_rec ON orders (tenant_id, recommendation_id);
CREATE INDEX ix_orders_tenant_created ON orders (tenant_id, created_at DESC);
CREATE SEQUENCE order_number_seq START 100000;

CREATE TABLE order_lines (
    order_line_id  UUID PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    order_id       UUID NOT NULL,
    line_no        INTEGER NOT NULL CHECK (line_no > 0),
    product_id     UUID NOT NULL,
    sku            TEXT NOT NULL,
    ordered_qty    INTEGER NOT NULL CHECK (ordered_qty > 0 AND ordered_qty <= 100000),
    unit_cost      NUMERIC(12,2) NOT NULL CHECK (unit_cost >= 0),
    line_total     NUMERIC(14,2) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, order_id, line_no),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, product_id) REFERENCES products(tenant_id, product_id),
    CHECK (line_total = round(ordered_qty * unit_cost, 2))
);
CREATE INDEX ix_order_lines_order ON order_lines (tenant_id, order_id);

CREATE TABLE payments (
    payment_id         UUID PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    order_id           UUID NOT NULL,
    amount             NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    currency           CHAR(3) NOT NULL DEFAULT 'USD',
    method             TEXT NOT NULL CHECK (method IN ('card','ach','invoice')),
    method_last4       CHAR(4),
    status             TEXT NOT NULL CHECK (status IN ('pending','authorized','captured','failed','refunded')),
    provider_reference TEXT,
    failure_code       TEXT,
    failure_reason     TEXT,
    attempt            INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    idempotency_key    TEXT NOT NULL,
    approval_id        UUID,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, payment_id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id),
    FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals(tenant_id, approval_id)
);
CREATE INDEX ix_payments_order ON payments (tenant_id, order_id, created_at DESC);
-- at most one successful (captured/authorized) payment per order: no double charge
CREATE UNIQUE INDEX ux_payments_one_success ON payments (tenant_id, order_id) WHERE status IN ('authorized','captured');

CREATE TABLE notifications (
    notification_id  UUID PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(tenant_id),
    order_id         UUID,
    channel          TEXT NOT NULL CHECK (channel IN ('email','sms','webhook','in_app')),
    recipient        TEXT NOT NULL,
    template         TEXT NOT NULL,
    payload          JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','sending','delivered','failed','dead_letter')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 5,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error       TEXT,
    idempotency_key  TEXT NOT NULL,
    delivered_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, notification_id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id)
);
CREATE INDEX ix_notifications_due ON notifications (status, next_attempt_at) WHERE status IN ('queued','failed');
CREATE INDEX ix_notifications_order ON notifications (tenant_id, order_id);

CREATE TABLE idempotency_keys (
    tenant_id     TEXT NOT NULL,
    scope         TEXT NOT NULL,
    key           TEXT NOT NULL CHECK (length(key) BETWEEN 8 AND 200),
    request_hash  CHAR(64) NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('in_progress','completed')),
    response      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, scope, key)
);

CREATE TABLE audit_events (
    event_id       BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT,
    actor_user_id  TEXT,
    actor_agent    TEXT,
    action         TEXT NOT NULL,
    resource_id    TEXT,
    outcome        TEXT NOT NULL CHECK (outcome IN ('success','failure','denied')),
    request_id     TEXT,
    trace_id       TEXT,
    agent_version  TEXT,
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_tenant_time ON audit_events (tenant_id, created_at DESC);
CREATE INDEX ix_audit_action ON audit_events (action, created_at DESC);

CREATE TABLE conversations (
    conversation_id  UUID PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(tenant_id),
    user_id          TEXT NOT NULL,
    title            TEXT,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
    summary          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, conversation_id)
);
CREATE INDEX ix_conversations_user ON conversations (tenant_id, user_id, updated_at DESC);

CREATE TABLE messages (
    message_id       UUID PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    conversation_id  UUID NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content          TEXT NOT NULL,
    intent           TEXT,
    trace_id         TEXT,
    request_id       TEXT,
    prompt_version   TEXT,
    agent_version    TEXT,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, message_id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations(tenant_id, conversation_id) ON DELETE CASCADE
);
CREATE INDEX ix_messages_conv ON messages (tenant_id, conversation_id, created_at);

CREATE TABLE llm_usage (
    usage_id        BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT,
    route           TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    outcome         TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cached_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(12,6) NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    cache_hit       BOOLEAN NOT NULL DEFAULT false,
    prompt_version  TEXT,
    agent_version   TEXT,
    trace_id        TEXT,
    request_id      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_llm_usage_tenant_day ON llm_usage (tenant_id, created_at);
CREATE INDEX ix_llm_usage_request ON llm_usage (request_id);
"""

TRIGGER_TABLES = ["vendors", "products", "recommendations", "orders", "payments", "notifications",
                  "idempotency_keys", "conversations"]


def upgrade() -> None:
    op.execute(UPGRADE)
    for table in TRIGGER_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS llm_usage, messages, conversations, audit_events, idempotency_keys,
            notifications, payments, order_lines, orders, approvals, recommendations,
            inventory_positions, locations, products, vendors, users, tenants CASCADE;
        DROP SEQUENCE IF EXISTS order_number_seq;
        DROP FUNCTION IF EXISTS set_updated_at();
        """
    )
