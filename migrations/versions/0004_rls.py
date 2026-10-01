"""Row-level security and least-privilege application role.

* Services connect as ``app_user`` (not the table owner), so RLS applies to them.
* Each transaction sets ``app.tenant_id`` (see ``ordering.common.db.tenant_session``).
* ``audit_events`` and ``memory_history`` are append-only for the app role.
* Shared RAG documents (``tenant_id IS NULL``) are readable by every tenant; RAG writes are
  owner-only (ingestion runs with the admin role).

Revision ID: 0004_rls
Revises: 0003_memory_agentops
Create Date: 2026-09-30
"""

from alembic import op

revision = "0004_rls"
down_revision = "0003_memory_agentops"
branch_labels = None
depends_on = None

TENANT_TABLES = [
    "users", "vendors", "products", "locations", "inventory_positions", "recommendations", "approvals",
    "orders", "order_lines", "payments", "notifications", "idempotency_keys", "conversations", "messages",
    "memories", "feedback", "eval_candidates",
]
CURRENT = "NULLIF(current_setting('app.tenant_id', true), '')"


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                -- normally pre-created by infra (docker/postgres/init.sh) with a secret password
                CREATE ROLE app_user NOLOGIN;
            END IF;
        END $$;
        GRANT USAGE ON SCHEMA public TO app_user;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
        REVOKE UPDATE, DELETE ON audit_events, memory_history, llm_usage FROM app_user;
        REVOKE INSERT, UPDATE, DELETE ON rag_documents, rag_document_versions, rag_chunks,
            rag_embeddings_openai, rag_embeddings_gemini, rag_embeddings_local, rag_embeddings_hashing,
            tenants, eval_runs FROM app_user;
        """
    )
    for table in TENANT_TABLES:
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = {CURRENT})
                WITH CHECK (tenant_id = {CURRENT});
            """
        )
    # Append-only logs: rows may be written without a tenant (system events) but are only
    # readable within the current tenant.
    for table in ["audit_events", "llm_usage", "memory_history"]:
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            CREATE POLICY tenant_read ON {table} FOR SELECT USING (tenant_id = {CURRENT});
            CREATE POLICY tenant_insert ON {table} FOR INSERT
                WITH CHECK (tenant_id IS NULL OR tenant_id = {CURRENT});
            """
        )
    # Shared + tenant RAG content
    for table in ["rag_documents", "rag_chunks"]:
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            CREATE POLICY tenant_or_shared ON {table} FOR SELECT
                USING (tenant_id IS NULL OR tenant_id = {CURRENT});
            """
        )
    # tenants: a tenant can read only its own row
    op.execute(
        f"""
        ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
        CREATE POLICY own_tenant ON tenants FOR SELECT USING (tenant_id = {CURRENT});
        """
    )
    # Worker-only cross-tenant access to the notification queue uses a SECURITY DEFINER
    # function so the worker still connects as app_user.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION claim_due_notifications(batch_size INTEGER)
        RETURNS SETOF notifications LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            UPDATE notifications n SET status = 'sending', attempts = n.attempts + 1, updated_at = now()
            WHERE n.notification_id IN (
                SELECT notification_id FROM notifications
                WHERE status IN ('queued','failed') AND next_attempt_at <= now()
                ORDER BY next_attempt_at
                FOR UPDATE SKIP LOCKED
                LIMIT batch_size)
            RETURNING n.*;
        $$;
        REVOKE ALL ON FUNCTION claim_due_notifications(INTEGER) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION claim_due_notifications(INTEGER) TO app_user;

        CREATE OR REPLACE FUNCTION reclaim_stuck_notifications(stuck_seconds INTEGER)
        RETURNS INTEGER LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            WITH r AS (
                UPDATE notifications SET status = 'failed', next_attempt_at = now(),
                    last_error = 'worker lease expired'
                WHERE status = 'sending' AND updated_at < now() - make_interval(secs => stuck_seconds)
                RETURNING 1)
            SELECT count(*)::INTEGER FROM r;
        $$;
        REVOKE ALL ON FUNCTION reclaim_stuck_notifications(INTEGER) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION reclaim_stuck_notifications(INTEGER) TO app_user;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claim_due_notifications(INTEGER), reclaim_stuck_notifications(INTEGER)")
    for table in [*TENANT_TABLES, "rag_documents", "rag_chunks", "tenants", "audit_events", "llm_usage",
                  "memory_history"]:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"DROP POLICY IF EXISTS tenant_or_shared ON {table}")
        op.execute(f"DROP POLICY IF EXISTS own_tenant ON {table}")
        op.execute(f"DROP POLICY IF EXISTS tenant_read ON {table}")
        op.execute(f"DROP POLICY IF EXISTS tenant_insert ON {table}")
