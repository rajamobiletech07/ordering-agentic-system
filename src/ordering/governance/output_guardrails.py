"""Output guardrails applied to every assistant reply (supervisor ``finalize`` node).

* Secret / payment-data leakage → redacted (JWTs, API keys, bearer tokens, card numbers).
* Policy answers without citations → replaced by an explicit insufficient-evidence reply
  (``content.require_citations_for_policy_answers``).
* Hits are counted in metrics and surfaced in the response metadata for audit.
"""

from __future__ import annotations

from ordering.common.metrics import GUARDRAIL_HITS
from ordering.common.redaction import redact_text
from ordering.governance.policy import get_policy

_LEAK_CLASSES = {"jwt", "bearer", "api_key", "card"}


def check_reply(response: dict) -> dict:
    reply = response.get("reply", "")
    hits: list[str] = []
    cleaned = redact_text(reply, classes=_LEAK_CLASSES)
    if cleaned != reply:
        hits.append("secret_leak_redacted")
        reply = cleaned
    policy = get_policy().content
    if (policy.require_citations_for_policy_answers and response.get("intent") == "policy_question"
            and response.get("evidence") != "insufficient" and not response.get("citations")
            and response.get("status") == "completed"):
        from ordering.rag.answerer import INSUFFICIENT

        hits.append("uncited_policy_answer_blocked")
        reply = INSUFFICIENT
    for hit in hits:
        GUARDRAIL_HITS.labels(hit, "output").inc()
    if hits:
        response = {**response, "reply": reply, "data": {**response.get("data", {}), "output_guardrails": hits}}
    return response
