# Model card - LLM routes

All model access goes through the LLM Gateway. Every route lists three models from three providers; on
timeout / 5xx / 429 / refusal / invalid structured output the gateway retries once (transient errors only)
and then falls back to the next provider. Route config: `src/ordering/llm_gateway/config/routes.yaml`
(versioned; stamped on every `llm_usage` row).

| Route | Primary | Fallback 1 | Fallback 2 | Output | Used by |
|---|---|---|---|---|---|
| intent_classification | claude-haiku-4-5 | gpt-5.4-mini | gemini-3.8-flash | JSON (Intent) | Supervisor |
| planning | claude-sonnet-5-5 (effort medium) | gpt-6-astra | gemini-3.1-pro-preview | JSON (InvestigationStep) | Recommendation Agent |
| explanation | claude-sonnet-5-5 (effort low) | gpt-6-astra | gemini-3.1-pro-preview | text | Recommendation Agent |
| rag_answer | claude-sonnet-5-5 (effort low) | gpt-6-astra | gemini-3.1-pro-preview | JSON (GroundedAnswer) | Recommendation Agent |
| summarization | claude-haiku-4-5 | gpt-5.4-mini | gemini-3.8-flash | text | Short-term memory |
| memory_extraction | claude-haiku-4-5 | gpt-5.4-mini | gemini-3.8-flash | JSON (MemoryOps) | Long-term memory |
| injection_judge | claude-haiku-4-5 | gpt-5.4-mini | gemini-3.8-flash | JSON (InjectionVerdict) | Guardrails |
| eval_judge | claude-sonnet-5-5 (effort medium) | gpt-6-astra | gemini-3.1-pro-preview | JSON (GroundednessVerdict) | Evals |

**Embeddings** (no cross-provider fallback - vector spaces differ): OpenAI `text-embedding-3-small` (default,
1536d), Gemini `gemini-embedding-2` (768d), local `BAAI/bge-small-en-v1.5` (384d). Reranker: local
`Xenova/ms-marco-MiniLM-L-6-v2`.

**Intended use**: classification, explanation and grounded Q&A inside the ordering assistant. **Out of scope**:
making decisions with financial effect (always human-approved), forecasting, legal/HR advice.

**Evaluation**: `evals/` suites (intent, injection, rag, groundedness) with baselines gate every change.

**Data**: prompts contain tenant business data (SKUs, quantities, prices) but no payment credentials; PII is
redacted from memory, logs and traces. Tenants can be restricted to specific providers
(`governance/policy.yaml → tenants.*.allowed_providers`).
