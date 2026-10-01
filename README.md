# Ordering Agentic System

An end-to-end, production-structured **agentic ordering chatbot** for retail replenishment:

* API Gateway → **LangGraph Supervisor** → **Recommendation Agent** + **Ordering Agent**
* **four MCP servers**: recommendation, ordering, payment (simulated), notification
* PostgreSQL + pgvector with row-level security
* an **LLM Gateway** with three providers per request type (Claude → OpenAI → Gemini) and automatic fallback
* production **RAG** with citations
* human approvals, idempotency, durable workflow state, short- and long-term memory, per-thread cache with mandatory cleanup
* OpenTelemetry sidecars, Prometheus/Grafana, and an AgentOps feedback loop with CI/CD

Everything runs locally with Docker Compose, **with no API keys** (a deterministic mock provider), or with real Claude, OpenAI and Gemini keys.

> Architecture, diagrams and design decisions: **[ARCHITECTURE.md](ARCHITECTURE.md)** · Example chats: **[examples/conversations.md](examples/conversations.md)** · API examples: **[examples/api_requests.http](examples/api_requests.http)** · Governance: **[governance/](governance/)**

---

## 1. Quick start

**Prerequisites**:

- Docker Desktop with Compose v2, 6 GB RAM for Docker
- Python 3.12 and [uv](https://docs.astral.sh/uv/), for tests and scripts on the host
- Free ports: 8000, 8010, 8020, 9001-9004, 55432, 6379, 3000, 9090, 16686, 4317

```bash
cp .env.example .env
```

```bash
make venv
```

```bash
make up
```

`make up` builds one image. It then starts Postgres, Redis, the 4 MCP servers, the notification worker, the LLM gateway, the agent service, the API gateway, the OpenTelemetry sidecars and collector, Jaeger, Prometheus and Grafana. It also runs three one-shot jobs automatically:

- `migrate`: Alembic migrations 0001-0005
- `seed`: about 1,000 rows per core table (idempotent)
- `rag-ingest`: sample policy documents

Once `make up` prints `ready` for every service, run the scripted demo:

```bash
make demo
```

```bash
make demo-failures
```

| URL | What |
|---|---|
| http://localhost:8000/docs | API gateway (OpenAPI UI) |
| http://localhost:16686 | Jaeger - distributed traces (search service `api-gateway`) |
| http://localhost:3000 | Grafana (admin / admin) → *Ordering - AgentOps* dashboard |
| http://localhost:9090 | Prometheus |
| http://localhost:8020/readyz | LLM gateway provider health |

### Using real LLMs

In `.env` set `LLM_PROVIDER_OVERRIDE=` (empty) and add any of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `GOOGLE_API_KEY`. Providers without a key are skipped (`not_configured`), and the next provider in the chain answers. Default embeddings are OpenAI `text-embedding-3-small`, so set `OPENAI_API_KEY`, or switch `EMBEDDING_PROFILE=local`, which runs a fully local ONNX model with no key. Then restart:

```bash
docker compose up -d --force-recreate llm-gateway agent-service rag-ingest
```

To check that every configured model is reachable:

```bash
make probe-models
```

To use LiteLLM instead of the in-house adapters, set `LLM_BACKEND=litellm` and run:

```bash
docker compose --profile litellm up -d
```

---

## 2. Chat through the API

```bash
TOKEN=$(curl -s -X POST localhost:8000/v1/auth/dev-token -H 'Content-Type: application/json' -d '{"user_id":"u-acme-buyer"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
```

```bash
curl -s localhost:8000/v1/chat -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"message":"What should I order for STORE-014?"}' | python3 -m json.tool
```

Continue the conversation by passing `"conversation_id"`. A proposal returns `status: "awaiting_approval"` and a `pending_approval` object. Reply `"yes"`, or call `POST /v1/approvals/{approval_id}` with `{"decision":"approve"}`. Send an `Idempotency-Key` header on confirmations so client retries are safe.

**Seeded users** (use them with `/v1/auth/dev-token`, which only exists when `ENV` is not production):

| user_id | tenant | roles |
|---|---|---|
| `u-acme-buyer` | acme-retail | buyer |
| `u-acme-approver` | acme-retail | buyer, approver |
| `u-acme-viewer` | acme-retail | viewer |
| `u-acme-admin` | acme-retail | admin |
| `u-globex-buyer` / `u-globex-admin` | globex-grocers | buyer / admin |

**Demo fixtures**:

| Fixture | Scenario |
|---|---|
| SKU-10042 @ STORE-014 | critical low stock, $1,386 |
| SKU-10077 @ DC-02 | $39,200, needs a different approver |
| SKU-10108 @ STORE-003 | high risk |
| GLX-20015 @ STORE-G07 | Globex tenant |

**Simulated payment tokens**:

| Token | Outcome |
|---|---|
| `tok_visa`, `tok_mastercard`, `tok_amex` | capture |
| `tok_decline` | card_declined |
| `tok_insufficient` | insufficient_funds |
| `tok_processor_error` | retryable outage |

---

## 3. Services

| Service | Port | Role |
|---|---|---|
| api-gateway (+ otel sidecar) | 8000 | Public edge: JWT, RBAC, validation, rate limiting, tracing |
| agent-service (+ otel sidecar) | 8010 | LangGraph supervisor, Recommendation & Ordering agents, memory, approvals |
| llm-gateway | 8020 | 3-provider routing/fallback, budgets, cost ledger, embeddings |
| mcp-recommendation | 9001 | recommendations, inventory positions/risks, vendors (read-only) |
| mcp-ordering | 9002 | create/read/update/cancel orders |
| mcp-payment | 9003 | simulated payments + status |
| mcp-notification | 9004 | enqueue simulated notifications + delivery status |
| notification-worker | - | async delivery, retries, dead-lettering |
| postgres (pgvector) | 55432 | system of record, RAG vectors, checkpoints, memory |
| redis | 6379 | rate limits, thread cache, locks, budgets, runtime fault flags |
| litellm (profile `litellm`) | 4000 | optional LLM backend |

Locally, that's **16 long-running containers**: the 8 application services, 2 telemetry sidecars, Postgres and Redis,
and the OTel collector, Jaeger, Prometheus and Grafana. The one-shot `migrate`, `seed` and `rag-ingest` jobs
exit when done. On GKE, the same 8 application services run as Deployments, and Postgres, Redis, tracing,
metrics and logs become managed Google Cloud services. See [docs/deployment/gcp-gke.md §1](docs/deployment/gcp-gke.md#1-how-many-services-run).

The **agent → tool allowlist** is enforced in code (`src/ordering/security/permissions.py`):

- **Recommendation Agent**: `recommendation.*` (read) and `ordering.list_orders`.
- **Ordering Agent**: `ordering.*`, `payment.*`, `notification.*`, and `recommendation.get_recommendation` / `get_vendor`.
- **Supervisor**: no tools.

---

## 4. Tests, evals and security scans

```bash
make test-unit
```

```bash
make test-integration
```

```bash
make test-e2e
```

```bash
make evals
```

```bash
make lint typecheck security
```

What each suite needs and covers:

- **Unit** (69 tests, no infrastructure): decorators, security, LLM routing/fallback, RAG pieces, guardrails, cache, contracts, prompts.
- **Integration** (12 tests, needs Postgres + Redis): real MCP servers in-process, approvals, idempotency, no double charge, RLS, seeding, RAG versioning/evals, memory.
- **End-to-end** (14 tests, needs the full stack): the demo workflow and every failure mode through the public API.
- **Evals**: intent/route accuracy, injection, RAG recall/MRR/refusal/leakage, groundedness. The run fails on a regression against `evals/baselines/*.json`.
- **Scans**: ruff, mypy, bandit, pip-audit, and gitleaks if installed.

Integration tests only need the data stores:

```bash
docker compose up -d postgres redis && make migrate seed
```

You can run the services as local processes instead of containers. This needs the data stores above, and is fast for debugging:

```bash
LLM_PROVIDER_OVERRIDE=mock EMBEDDING_PROFILE=hashing make dev-up
```

---

## 5. RAG

```bash
make ingest
```

```bash
python -m ordering.rag.cli search "approval threshold" --tenant acme-retail
```

```bash
python -m ordering.rag.cli ask "What happens when a payment is declined?" --tenant acme-retail
```

```bash
python -m ordering.rag.cli eval --set data/rag_eval/eval_set.jsonl
```

`make ingest` is idempotent: it back-fills missing embeddings, versions changed documents, and skips duplicates.

Sample corpus in `data/rag_docs/`:

- shared policies: ordering (v1 in `_archive` and v2), vendor rules, replenishment, payment, operations;
- tenant addenda;
- a duplicate copy, for duplicate detection;
- a prompt-injection document, which the pipeline treats as data.

Embedding profiles:

| Profile | Model | Dimensions | Notes |
|---|---|---|---|
| `openai` (default) | text-embedding-3-small | 1536 | via the LLM gateway |
| `gemini` | gemini-embedding-2 | 768 | via the LLM gateway |
| `local` | fastembed BAAI/bge-small-en-v1.5 | 384 | baked into the image, no key |
| `hashing` | deterministic feature hashing | - | offline tests |

Each profile has its own pgvector table. Switching `EMBEDDING_PROFILE` needs no re-ingest if that profile was ingested; compose ingests the configured profile plus `local` and `hashing`.

Retrieval steps:

1. Tenant filter, RLS plus an explicit SQL filter: own tenant's documents plus shared documents.
2. Current versions only.
3. Vector top-20 and full-text top-20.
4. RRF fusion.
5. Cross-encoder rerank.
6. Evidence threshold.
7. A grounded answer with validated `[n]` citations, or an explicit "insufficient evidence" reply.

---

## 6. AgentOps: feedback loop, versioning, rollouts

1. **Capture.** `POST /v1/conversations/{id}/feedback`. Implicit signals are captured automatically: guardrail blocks, refusals, degraded LLM calls. They go to the `feedback` and `eval_candidates` tables, redacted and linked to the trace, prompt version and agent version.
2. **Mine.** Export candidates for review:

   ```bash
   python -m ordering.agentops.feedback export --out reports/eval_candidates.jsonl
   ```

3. **Curate.** Promote a reviewed case into a versioned dataset:

   ```bash
   python -m ordering.agentops.feedback promote <candidate_id> --suite intent --expected-intent policy_question
   ```

4. **Evaluate.** Run `make evals`. CI runs `evals.yml` on PRs that touch prompts, agents, RAG, the LLM gateway or governance.
5. **Ship.** Prompts are versioned in `src/ordering/prompts/library/*.yaml`. Roll a version out as a canary and roll back instantly:

   ```bash
   python -m ordering.prompts.rollouts set intent_classifier 1.0.0 20
   ```

   ```bash
   python -m ordering.prompts.rollouts clear intent_classifier
   ```

   The nightly `feedback-loop.yml` runs steps 2 and 4.

CI/CD lives in `.github/workflows/`:

| Workflow | What it does |
|---|---|
| `ci.yml` | lint, types, unit tests with coverage, compose-based integration + E2E + evals |
| `security.yml` | bandit, semgrep, pip-audit, gitleaks, hadolint, trivy image + IaC scans, SBOM |
| `evals.yml` | eval regression gate on PRs |
| `release.yml` | cosign-signed images and an OpenAPI release |
| `feedback-loop.yml` | nightly steps 2 and 4 above |

Also included: `.pre-commit-config.yaml`, Dependabot and CODEOWNERS.

---

## 7. Configuration

All settings come from environment variables or `.env`. See [.env.example](.env.example) for the full list with comments. Any secret can instead be given as a file via `<NAME>_FILE`, for Docker/Kubernetes secrets. In production the service refuses dev secrets and the dev-token endpoint.

Key settings:

| Setting | What it controls |
|---|---|
| `LLM_BACKEND` | `native` or `litellm` |
| `LLM_PROVIDER_OVERRIDE` | `mock` for keyless runs |
| `EMBEDDING_PROFILE` | which embedding profile retrieval uses |
| `RERANKER_ENABLED` | cross-encoder reranking on/off |
| `THREAD_CACHE_TTL_S` | per-thread cache TTL |
| `SHORT_TERM_WINDOW_TURNS` | short-term memory window |
| `MEMORY_RETENTION_DAYS` | long-term memory retention |
| `RATE_LIMIT_PER_MINUTE` | per-user rate limit |
| `TENANT_DAILY_BUDGET_USD` | per-tenant daily LLM budget |
| `OTEL_ENABLED` | OpenTelemetry export on/off |

Model routes and prices live in `src/ordering/llm_gateway/config/routes.yaml`. Governance rules (approval thresholds, PII classes, memory retention, provider allowlists per tenant) live in `governance/policy.yaml`.

---

## 8. Project layout

```
src/ordering/
  api_gateway/     public FastAPI edge (auth, rate limit, validation, proxy)
  agents/          service.py, supervisor/ (LangGraph), recommendation/, ordering/, mcp_client.py, cache.py, guardrails.py, contracts.py
  mcp_servers/     recommendation/, ordering/, payment/ (+ simulator), notification/, common.py
  notification_worker/  async delivery worker + simulated channels
  llm_gateway/     providers/ (anthropic, openai, gemini, mock), backends/ (native, litellm), router, usage, config/routes.yaml
  llm_client/      agent-side gateway client (structured outputs, degradation, thread cache)
  rag/             loaders, chunker, embeddings/, ingest, retriever, reranker, answerer, eval, cli
  memory/          short_term.py, long_term.py
  prompts/         registry.py, rollouts.py, library/*.yaml (versioned prompts)
  agentops/        feedback.py, evals/ (scorers, runner)
  governance/      policy.py, output_guardrails.py
  decorators/      auth, tool_access, observability, errors, resilience, idempotency
  security/        jwt, service_tokens, permissions, dependencies
  common/          config, db (tenant sessions), logging, redaction, tracing, metrics, context, errors, redis, faults
  repositories/    SQL access per aggregate
  domain/schemas/  typed contracts (pydantic)
migrations/        Alembic 0001-0005 (schema, RAG, memory/AgentOps, RLS, default privileges)
scripts/           seed.py, demo.py, issue_token.py, dev_local.sh, export_openapi.py, wait_for_services.py
data/              rag_docs/ (sample corpus), rag_eval/ (retrieval eval set)
evals/             datasets/ + baselines/
docker/            app.Dockerfile, otel sidecar/collector, prometheus, grafana, litellm, postgres init
deploy/gcp/        Terraform (GKE Autopilot, Cloud SQL, Memorystore, WIF, Cloud Armor) + bootstrap scripts
deploy/k8s/        Kustomize base/overlays for GKE + render/deploy/run-job scripts
governance/        policy.yaml, agent/model cards, risk register, data handling, incident runbook
tests/             unit/, integration/, e2e/
```

### Reused from `mcp-core` (copied and adapted, no runtime dependency)

| Adapted into | From |
|---|---|
| FastMCP server scaffold with health route | `inventory_smart_server/fastmcp_server.py` |
| `@traced`/`@audited`, based on `log_mcp_tool` | `decorator.py` |
| `@agent_tool` auth | `auth_decorator.py` |
| LLM provider adapters and factory | `python_code_gen_server/llm_engine/*` |
| Route table | `llm_wrapper/config.json` |
| pgvector store, embedding interface, memory ADD/UPDATE/DELETE flow | `memory_server/*` |
| MCP client pattern | `streamable_http_template/server_usage_template.py` |

Fixed while adapting:

- hard-coded keys;
- a global mutable headers dict;
- blocking `requests` inside async code;
- blanket retries on non-idempotent calls;
- per-call remote token verification;
- fastmcp ≥ 4 stripping `Authorization` from `get_http_headers()` (now opted in explicitly).

---

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| Port 5432 already in use | Compose publishes Postgres on **55432** on purpose; host URLs in `.env` already use it. |
| `make up` fails with a `rag-ingest` error | With `EMBEDDING_PROFILE=openai` and no mock, OpenAI needs `OPENAI_API_KEY`. Set the key, use `LLM_PROVIDER_OVERRIDE=mock`, or use `EMBEDDING_PROFILE=local`. |
| Replies say `degraded` | No provider answered. Check `curl localhost:8020/readyz`: providers show `configured:false` without keys. Mock mode avoids this. |
| `409 another turn is still being processed` | Two requests hit the same conversation concurrently; retry after the first finishes. |
| Proposal stays `awaiting_approval` after "yes" | Orders above the tenant threshold need a *different* user with the `approver` role. Use `POST /v1/approvals/{id}` as `u-acme-approver`. |
| "payment service is temporarily unavailable… recovering" | The circuit breaker is open after an outage; it half-opens after about 10 s. Retry then; retries are idempotent. |
| Demo fixtures already ordered | Re-run `make seed` (idempotent). It resets fixture recommendations to `open`. |
| No traces in Jaeger | Set `OTEL_ENABLED=true` in `.env` and recreate the containers. |
| Integration/E2E tests are skipped | They auto-skip when Postgres (55432), Redis (6379) or the gateway (8000) are unreachable. |
| Start from scratch | `docker compose down -v`, then `make up`. This drops the database and caches. |

## 10. Production notes

Tested locally: everything in this README. For production deployment you still need the items in [ARCHITECTURE.md §11](ARCHITECTURE.md#11-implemented-and-tested-locally-vs-production-additions):

- managed HA Postgres and Redis
- an OIDC IdP with JWKS and an auth proxy
- a secrets manager
- mTLS
- Kubernetes: the Google Cloud path (GKE, Terraform, GitHub Actions CI/CD) is ready to apply; see
  [docs/deployment/gcp-gke.md](docs/deployment/gcp-gke.md)
- a real PSP with reconciliation
- SLOs and alerting
- load and chaos testing
- provider data-retention agreements
