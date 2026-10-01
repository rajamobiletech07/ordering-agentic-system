"""Short-term (thread) memory: a bounded message window plus a rolling summary.

The LangGraph checkpoint (Postgres) is the durable store. When the thread exceeds
``SHORT_TERM_WINDOW_TURNS`` turns, the older messages are folded into ``summary`` (LLM route
``summarization`` with a deterministic fallback) and dropped from the window, keeping prompts
small and predictable. Structured facts (current recommendation, proposal, order) live in
dedicated state fields and are never re-derived from text.
"""

from __future__ import annotations

from ordering.common.config import get_settings
from ordering.common.redaction import redact_text
from ordering.llm_client.client import get_llm_client
from ordering.prompts import registry


def _fallback_summary(previous: str, turns: list[dict]) -> str:
    snippets = [f"{m['role']}: {m['content'][:140]}" for m in turns if m["role"] in ("user", "assistant")]
    text = (previous + " | " if previous else "") + " | ".join(snippets)
    return redact_text(text)[-1200:]


async def compact_window(messages: list[dict], summary: str, sticky_key: str | None = None) -> tuple[list[dict], str, bool]:
    """Returns (window, summary, compacted?)."""
    max_messages = get_settings().short_term_window_turns * 2
    if len(messages) <= max_messages:
        return messages, summary, False
    older, window = messages[:-max_messages], messages[-max_messages:]
    prompt = await registry.resolve("conversation_summarizer", sticky_key)
    turns = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in older)
    system, user = prompt.render(previous=summary or "(none)", turns=turns)
    result = await get_llm_client().complete(prompt.route, system=system, user=user,
                                             fallback=_fallback_summary(summary, older),
                                             prompt_id=prompt.prompt_id, prompt_version=prompt.version)
    return window, redact_text(result.text.strip())[:2000], True


def state_brief(state: dict) -> str:
    """Compact, structured conversation state for prompts (no free-text history)."""
    parts = []
    if state.get("summary"):
        parts.append(f"summary: {state['summary'][:600]}")
    recs = state.get("last_recommendations") or []
    if recs:
        parts.append("listed recommendations: " + "; ".join(f"{i}. {r['sku']}@{r['location_code']}" for i, r in enumerate(recs, 1)))
    if state.get("pending_approval"):
        parts.append(f"pending approval: {state['pending_approval']['action']} - {state['pending_approval']['summary'][:200]}")
    if state.get("order"):
        o = state["order"]
        parts.append(f"current order: {o.get('order_number')} status={o.get('status')} payment={o.get('payment_status')}")
    if state.get("memories"):
        parts.append("user preferences: " + "; ".join(m["content"] for m in state["memories"][:5]))
    return " | ".join(parts) or "new conversation"
