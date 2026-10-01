# Incident runbook

| Symptom | Where to look | Immediate action |
|---|---|---|
| Replies marked `degraded` | Grafana → "LLM attempts by provider / outcome", "Fallbacks" | Check provider status; gateway already falls back. If all providers fail, templates keep the product usable. |
| `awaiting_retry` after confirmation | Jaeger trace of the turn (`x-trace-id` header) → failing `mcp.call` span | Restore the dependency; users say "retry payment" (idempotent, never double-charges) or call `POST /v1/conversations/{id}/resume`. |
| Suspected prompt-injection attack | Grafana "Guardrail detections"; `audit_events` | Raise `injection.block_threshold` sensitivity in `governance/policy.yaml` (review required); add cases to `evals/datasets/injection.jsonl`. |
| Wrong answer reported | `feedback` + `eval_candidates` tables | `python -m ordering.agentops.feedback export`, triage, `promote` into datasets; fix prompt; canary via `python -m ordering.prompts.rollouts set`. |
| Bad prompt release | Eval report / feedback spike | `python -m ordering.prompts.rollouts clear <prompt_id>` (instant rollback to default version). |
| Notification backlog | `notifications` where status in (`failed`,`dead_letter`) | Check worker logs; dead letters are retained for replay. |
| Cross-tenant data concern | RLS policies (`migrations/versions/0004_rls.py`), audit trail | Revoke tokens (rotate `JWT_SECRET`/JWKS), preserve audit evidence, notify security. |
