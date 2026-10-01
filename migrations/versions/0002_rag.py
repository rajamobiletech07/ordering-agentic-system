"""RAG schema: documents, versions (dedup by content hash), chunks with full-text index,
and one pgvector table per embedding profile (vector spaces are not interchangeable, and
HNSW indexes require a fixed dimension).

``tenant_id IS NULL`` marks a document shared by all tenants.

Revision ID: 0002_rag
Revises: 0001_core
Create Date: 2026-09-30
"""

from alembic import op

revision = "0002_rag"
down_revision = "0001_core"
branch_labels = None
depends_on = None

EMBEDDING_PROFILES = {"openai": 1536, "gemini": 768, "local": 384, "hashing": 256}

UPGRADE = r"""
CREATE TABLE rag_documents (
    document_id  UUID PRIMARY KEY,
    tenant_id    TEXT REFERENCES tenants(tenant_id),
    source_uri   TEXT NOT NULL,
    doc_type     TEXT NOT NULL CHECK (doc_type IN
                 ('ordering_policy','vendor_rules','replenishment_guideline','payment_policy','operations','other')),
    title        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_rag_doc_source ON rag_documents (COALESCE(tenant_id, '__shared__'), source_uri);

CREATE TABLE rag_document_versions (
    version_id      UUID PRIMARY KEY,
    document_id     UUID NOT NULL REFERENCES rag_documents(document_id) ON DELETE CASCADE,
    version_no      INTEGER NOT NULL CHECK (version_no > 0),
    declared_version TEXT,
    content_sha256  CHAR(64) NOT NULL,
    effective_date  DATE,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_current      BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version_no),
    UNIQUE (document_id, content_sha256)
);
CREATE UNIQUE INDEX ux_rag_one_current ON rag_document_versions (document_id) WHERE is_current;

CREATE TABLE rag_chunks (
    chunk_id      UUID PRIMARY KEY,
    version_id    UUID NOT NULL REFERENCES rag_document_versions(version_id) ON DELETE CASCADE,
    document_id   UUID NOT NULL REFERENCES rag_documents(document_id) ON DELETE CASCADE,
    tenant_id     TEXT,
    chunk_index   INTEGER NOT NULL,
    section_path  TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL,
    token_count   INTEGER NOT NULL,
    chunk_sha256  CHAR(64) NOT NULL,
    is_current    BOOLEAN NOT NULL DEFAULT true,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    tsv           TSVECTOR GENERATED ALWAYS AS (
                      setweight(to_tsvector('english', coalesce(section_path, '')), 'A') ||
                      setweight(to_tsvector('english', content), 'B')) STORED,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (version_id, chunk_index)
);
CREATE INDEX ix_rag_chunks_tsv ON rag_chunks USING GIN (tsv);
CREATE INDEX ix_rag_chunks_filter ON rag_chunks (tenant_id, is_current);
CREATE INDEX ix_rag_chunks_meta ON rag_chunks USING GIN (metadata jsonb_path_ops);
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    for profile, dims in EMBEDDING_PROFILES.items():
        op.execute(
            f"""
            CREATE TABLE rag_embeddings_{profile} (
                chunk_id   UUID PRIMARY KEY REFERENCES rag_chunks(chunk_id) ON DELETE CASCADE,
                model      TEXT NOT NULL,
                embedding  vector({dims}) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX ix_rag_emb_{profile}_hnsw ON rag_embeddings_{profile}
                USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
            """
        )


def downgrade() -> None:
    for profile in EMBEDDING_PROFILES:
        op.execute(f"DROP TABLE IF EXISTS rag_embeddings_{profile}")
    op.execute("DROP TABLE IF EXISTS rag_chunks, rag_document_versions, rag_documents CASCADE")
