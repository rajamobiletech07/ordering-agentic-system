# AI risk register

| # | Risk | Likelihood | Impact | Controls (implemented) | Residual / production follow-up |
|---|---|---|---|---|---|
| R1 | Unauthorized order or payment triggered by the model | Low | High | Human approval bound to parameter hash; approvals verified server-side in the same DB transaction; agent→tool allowlist; roles; idempotency | Add step-up auth (MFA) for approvals above threshold |
| R2 | Prompt injection via user input or retrieved documents | Medium | High | Heuristic screening + LLM judge; untrusted content sanitised/delimited; structured outputs; tools chosen by deterministic plans; injection eval suite | Periodic red-teaming; managed guardrail service |
| R3 | Cross-tenant data exposure | Low | High | Tenant from verified token only; composite tenant FKs; Postgres RLS; tenant-namespaced cache; RAG tenant filter + leakage eval | Per-tenant encryption keys; checkpoint table tenant column |
| R4 | Hallucinated policy / numbers | Medium | Medium | Numbers only from MCP tools; citations required; insufficient-evidence refusal; groundedness eval | Human review of low-rated answers via feedback loop |
| R5 | Double charge / duplicate order | Low | High | Idempotency keys (API + tool + DB unique); one-success payment index; approval-derived keys on resume | Reconciliation job against PSP |
| R6 | LLM provider outage or degradation | Medium | Medium | 3-provider fallback per route; circuit breakers; deterministic degraded responses; budgets | Multi-region provider endpoints |
| R7 | PII leakage into logs/memory/traces | Low | Medium | Redaction processors (logs, audit, traces, memory); deny-listed memory content; card numbers blocked | DLP scanning of log sinks |
| R8 | Cost overrun | Medium | Low | Per-tenant daily budgets; cost ledger; Grafana cost panels; cheap models for light routes | Alerting on spend anomalies |
| R9 | Model/prompt regressions after change | Medium | Medium | Versioned prompts; CI eval gate vs baselines; canary rollouts with instant rollback | Online A/B metrics tied to rollouts |
| R10 | Over-reliance / automation bias | Medium | Medium | AI disclosure on every reply; explicit summaries before approval; four-eyes for high value | UX research, training |
