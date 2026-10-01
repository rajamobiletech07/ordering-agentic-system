# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/) and semantic versioning. The
`AGENT_VERSION` is stamped on every trace, `llm_usage` row, audit event and message.

## [Unreleased]

### Added
- Google Cloud deployment ([docs/deployment/gcp-gke.md](docs/deployment/gcp-gke.md)):
  - Terraform for GKE Autopilot, Cloud SQL (Postgres 16 + pgvector), Memorystore, Artifact Registry
    with mirrors, Secret Manager, Cloud Armor, and GitHub Workload Identity Federation.
  - Kustomize base and overlays with a Secret Manager CSI mount per workload, default-deny
    NetworkPolicies, HPAs/PDBs, native OTel sidecars, Managed Prometheus, and suspended CronJob
    templates for migrations, seeding, ingestion, smoke tests and ops, plus a nightly feedback export.
  - `deploy-gcp.yml` CI/CD: CI + eval gates → build → Trivy gate → cosign keyless signing → staging
    → approved production promotion of the same digest, with automatic rollback. `terraform.yml`
    covers infrastructure.
- `scripts/db_bootstrap.py` (least-privilege `app_user` on managed Postgres, verified non-privileged so
  RLS applies), `scripts/setup_checkpointer.py`, and `scripts/smoke_test.py`.
- Make targets `k8s-render`, `k8s-validate`, `tf-validate`, `iac-scan`.

### Changed
- The image runs as fixed UID/GID 10001, to match Kubernetes `securityContext`.
- `REDIS_URL` can be supplied as a file (`REDIS_URL_FILE`), since it carries the Memorystore AUTH string.
- `CHECKPOINTER_AUTO_SETUP=false` (Kubernetes) keeps owner DB credentials out of agent-service.
- Logs carry the Cloud Logging `severity` field and, with `GOOGLE_CLOUD_PROJECT` set, trace correlation.
- `ci.yml` / `evals.yml` are reusable (`workflow_call`) release gates; main-branch CI runs inside `deploy-gcp.yml`.
- `deploy/k8s` reference manifests replaced by the Kustomize layout.

## [1.0.0] - 2026-09-30

### Added
- API gateway (JWT, RBAC, validation, Redis rate limiting, tracing, RFC 9457 errors).
- LangGraph supervisor with interrupt-based human approvals and Postgres checkpoints.
- Recommendation Agent (recommendations, explanations, bounded risk investigation, RAG policy answers, proposals)
  and Ordering Agent (idempotent create → pay → notify → verify saga, retries, cancellations).
- MCP servers: recommendation, ordering, payment (simulated), notification; notification worker.
- LLM gateway: 3 providers per route (Claude → OpenAI → Gemini) with retry/fallback, budgets, cost ledger,
  structured-output validation, embeddings; LiteLLM backend option.
- RAG: ingestion with versioning and duplicate detection, 4 embedding profiles, hybrid retrieval, reranking,
  citations, insufficient-evidence handling, evals.
- Short- and long-term memory; per-thread cache with mandatory cleanup.
- Prompt registry with canary rollouts; feedback loop; eval suites with baselines.
- Guardrails: injection screening (incl. secret-exfiltration signal added from the first eval run), output
  redaction, uncited-policy blocking.
- Docker Compose stack with OpenTelemetry sidecars, Jaeger, Prometheus, Grafana; K8s reference manifests.
- GitHub Actions: CI, security, evals, release, nightly feedback loop.

### Prompt versions
- `intent_classifier` 1.1.0 (default; state-aware confirmations), 1.0.0 (baseline)
- all other prompts 1.0.0
