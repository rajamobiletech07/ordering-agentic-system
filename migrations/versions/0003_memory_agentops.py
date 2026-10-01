"""Long-term memory, feedback loop, evals and prompt rollouts.

Memory embeddings use an un-dimensioned ``vector`` column plus a ``profile`` column: per-user
memory sets are small, so an exact (filtered) scan is fast and no ANN index is needed.

Revision ID: 0003_memory_agentops
Revises: 0002_rag
Create Date: 2026-09-30
"""

from alembic import op

revision = "0003_memory_agentops"
down_revision = "0002_rag"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE memories (
    memory_id               UUID PRIMARY KEY,
    tenant_id               TEXT NOT NULL REFERENCES tenants(tenant_id),
    user_id                 TEXT,
    scope                   TEXT NOT NULL CHECK (scope IN ('user','tenant')),
    memory_type             TEXT NOT NULL CHECK (memory_type IN ('preference','fact','procedural')),
    content                 TEXT NOT NULL CHECK (length(content) <= 1000),
    content_sha256          CHAR(64) NOT NULL,
    confidence              NUMERIC(3,2) NOT NULL DEFAULT 0.80 CHECK (confidence BETWEEN 0 AND 1),
    source_conversation_id  UUID,
    embedding_profile       TEXT NOT NULL,
    embedding               vector,
    expires_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((scope = 'user') = (user_id IS NOT NULL))
);
CREATE UNIQUE INDEX ux_memories_dedup ON memories (tenant_id, COALESCE(user_id, ''), content_sha256);
CREATE INDEX ix_memories_owner ON memories (tenant_id, user_id);

CREATE TABLE memory_history (
    history_id   BIGSERIAL PRIMARY KEY,
    memory_id    UUID NOT NULL,
    tenant_id    TEXT NOT NULL,
    user_id      TEXT,
    event        TEXT NOT NULL CHECK (event IN ('ADD','UPDATE','DELETE','ERASE','EXPIRE')),
    old_content  TEXT,
    new_content  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_memory_history_owner ON memory_history (tenant_id, user_id, created_at DESC);

CREATE TABLE feedback (
    feedback_id      UUID PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(tenant_id),
    user_id          TEXT NOT NULL,
    conversation_id  UUID NOT NULL,
    message_id       UUID,
    rating           SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    labels           TEXT[] NOT NULL DEFAULT '{}',
    comment          TEXT CHECK (length(comment) <= 2000),
    trace_id         TEXT,
    prompt_version   TEXT,
    agent_version    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_feedback_tenant_time ON feedback (tenant_id, created_at DESC);

CREATE TABLE eval_candidates (
    candidate_id     UUID PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('feedback','fallback','refusal','tool_error','guardrail','approval_rejected')),
    conversation_id  UUID,
    message_id       UUID,
    trace_id         TEXT,
    input_text       TEXT NOT NULL,
    output_text      TEXT,
    labels           TEXT[] NOT NULL DEFAULT '{}',
    suite            TEXT,
    status           TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','promoted','dismissed')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source, message_id)
);

CREATE TABLE eval_runs (
    run_id           UUID PRIMARY KEY,
    suite            TEXT NOT NULL,
    prompt_versions  JSONB NOT NULL DEFAULT '{}'::jsonb,
    agent_version    TEXT NOT NULL,
    metrics          JSONB NOT NULL,
    baseline         JSONB,
    passed           BOOLEAN NOT NULL,
    report_path      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE prompt_rollouts (
    prompt_id    TEXT NOT NULL,
    version      TEXT NOT NULL,
    traffic_pct  INTEGER NOT NULL CHECK (traffic_pct BETWEEN 0 AND 100),
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (prompt_id, version)
);
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(
        "CREATE TRIGGER trg_memories_updated BEFORE UPDATE ON memories FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS prompt_rollouts, eval_runs, eval_candidates, feedback, memory_history, memories")
