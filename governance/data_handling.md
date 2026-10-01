# Data handling & privacy policy (implementation summary)

* **System of record**: orders, payments, notifications, recommendations live in PostgreSQL; the assistant
  always re-reads them through MCP tools. Chat transcripts are *not* authoritative.
* **Tenant isolation**: tenant identity comes only from verified tokens; enforced by RLS in the database,
  composite foreign keys, tenant-scoped caches and RAG filters.
* **PII & secrets**: redacted before logs, traces, audit events and long-term memory (`common/redaction.py`,
  classes in `governance/policy.yaml`). Card numbers in chat are blocked; only simulated payment tokens exist.
* **Long-term memory**: preferences only, never transactional facts; 180-day retention (`expires_at`); users can
  list, delete, or erase all memories (`DELETE /v1/memory`, also clears every thread cache). Erasure leaves only
  an `ERASE` marker in `memory_history`.
* **Thread cache**: TTL-bound, invalidated on mutations, cleared on close/delete/erasure; swept periodically.
* **LLM providers**: prompts include business data but no credentials; per-tenant provider allowlists support
  data-residency constraints. Provider retention settings (e.g. zero-data-retention agreements) must be
  configured contractually in production.
* **Audit**: append-only `audit_events` (app role cannot update/delete) with actor user, acting agent, request
  and trace IDs.
