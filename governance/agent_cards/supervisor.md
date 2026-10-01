# Agent card - Supervisor

| Field | Value |
|---|---|
| Purpose | Understand the buyer's intent, plan the workflow, delegate typed tasks to the Recommendation and Ordering agents, hold conversation state, gate consequential actions behind human approval, handle failures. |
| Autonomy level | **L2 - propose & execute with approval.** Read-only actions run autonomously; every order change and payment requires explicit human approval bound to the exact parameters. |
| Tools | None directly. Delegates via typed `TaskRequest` / `TaskResult` contracts (`agents/contracts.py`). |
| Models | `intent_classification` route (Claude Haiku 4.5 → GPT-5.4 mini → Gemini 3.8 Flash); deterministic rule classifier as fallback. |
| Human oversight | LangGraph `interrupt()` approval gate; approvals expire (30 min); high-value orders need a *different* user with the approver role. |
| State | Durable checkpoints (Postgres) after every node; conversation transcript; per-thread cache (TTL + mandatory cleanup). |
| Known limits | One intent per turn; multi-item orders are proposed one recommendation at a time; no free-form tool selection. |
| Failure behaviour | Dependency down → degraded answer, workflow left in `awaiting_retry`; LLM down → deterministic templates (flagged `degraded`). |
| Owner | conversational-ai · reviewed by ai-governance |
