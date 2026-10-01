# Agent card - Recommendation Agent

| Field | Value |
|---|---|
| Purpose | Retrieve and explain replenishment recommendations, investigate inventory risk, answer policy questions (RAG), build order proposals. |
| Autonomy level | **L1 - read-only.** Cannot create, change or pay for anything. |
| Tools (enforced in code) | `recommendation.list_recommendations`, `get_recommendation`, `get_inventory_position`, `list_inventory_risks`, `get_vendor`; `ordering.list_orders` (read). |
| Bounded loop | Risk investigation: max `autonomy.max_investigation_steps` (4) read-only tool calls chosen by the planner LLM, deterministic plan as fallback. |
| Grounding | Numbers come only from MCP tool results (system of record). Policy statements require retrieved citations; otherwise it answers "insufficient evidence". |
| Models | `explanation`, `rag_answer`, `planning` routes (Claude Sonnet 5.5 → GPT-6 Astra → Gemini 3.1 Pro). |
| Known limits | Explains the engine's recommendation; it does not re-forecast demand. |
| Owner | replenishment-ai |
