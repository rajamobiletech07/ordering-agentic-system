"""Prompt-injection defences.

Layered, and never the *only* control - authorization, tenant isolation and approval binding
are enforced in code regardless of what the model says:

1. **Input screening** - heuristic scoring of user input; high scores are blocked, borderline
   inputs get a second opinion from the ``injection_judge`` LLM route.
2. **Untrusted-content handling** - retrieved documents, tool outputs and memories are wrapped
   in delimited blocks and instruction-like lines are stripped before reaching a prompt.
3. **Structured outputs** - model decisions are parsed into pydantic schemas; tools are chosen
   by deterministic task plans, not free text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from ordering.common.logging import get_logger
from ordering.common.metrics import GUARDRAIL_HITS
from ordering.governance.policy import get_policy

log = get_logger("ordering.guardrails")

_SIGNALS: list[tuple[str, float, re.Pattern[str]]] = [
    ("override", 0.55, re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|all|earlier|system)\b.{0,20}\b(instructions?|rules?|prompts?|messages?)", re.I)),
    ("role_hijack", 0.45, re.compile(r"\b(you are now|act as|pretend to be|switch to|enter)\b.{0,30}\b(admin|developer|root|system|dan|jailbreak|god)\b", re.I)),
    ("prompt_exfil", 0.5, re.compile(r"\b(reveal|show|print|repeat|leak|dump)\b.{0,40}\b(system prompt|instructions|hidden prompt)\b", re.I)),
    ("secret_exfil", 0.8, re.compile(r"\b(reveal|show|print|give|send|leak|dump|what (is|are))\b.{0,30}\b(api[_ -]?keys?|secrets?|credentials|passwords?|access tokens?|private keys?)\b", re.I)),
    ("fake_authority", 0.35, re.compile(r"\b(system|admin|developer|anthropic|openai)\s*(message|override|notice|instruction)s?\s*:", re.I)),
    ("approval_bypass", 0.5, re.compile(r"\b(approve|confirm|pay|charge)\b.{0,30}\b(all|every|automatically|without (asking|approval|confirmation))\b", re.I)),
    ("tenant_escape", 0.5, re.compile(r"\b(tenant[_ ]?id|other tenant|another tenant|all tenants|switch tenant)\b", re.I)),
    ("price_tamper", 0.4, re.compile(r"\b(set|change|make)\b.{0,30}\b(price|amount|cost|total)\b.{0,20}\b(to )?(zero|0|\$?0\.0+|free)\b", re.I)),
    ("delimiter_spoof", 0.3, re.compile(r"</?(system|sources|records|user_message|assistant)>", re.I)),
    ("encoded_payload", 0.2, re.compile(r"\b(base64|rot13|hex)\b.{0,20}\b(decode|instructions?)\b", re.I)),
]
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


@dataclass
class ScreenResult:
    score: float
    signals: list[str] = field(default_factory=list)
    action: str = "allow"  # allow | review | block
    reason: str | None = None


def heuristic_score(text: str) -> ScreenResult:
    hits = [(name, weight) for name, weight, pattern in _SIGNALS if pattern.search(text)]
    score = min(1.0, sum(w for _, w in hits))
    return ScreenResult(score=round(score, 2), signals=[n for n, _ in hits])


class InjectionVerdict(BaseModel):
    is_injection: bool
    confidence: float
    reason: str


async def screen_input(text: str) -> ScreenResult:
    """Screen a user message. Also blocks raw card numbers (policy: tokenized methods only)."""
    policy = get_policy().injection
    result = heuristic_score(text)
    if _CARD.search(text) and any(len(re.sub(r"\D", "", m)) >= 13 for m in _CARD.findall(text)):
        result.signals.append("card_number")
        result.action, result.reason = "block", "Card numbers must never be shared in chat; use a saved, tokenized payment method."
    elif result.score >= policy.block_threshold:
        result.action, result.reason = "block", "The message looks like an attempt to override the assistant's safety rules."
    elif result.score >= policy.review_threshold:
        verdict = await _judge(text, result)
        if verdict.is_injection and verdict.confidence >= 0.6:
            result.action, result.reason = "block", "The message looks like an attempt to override the assistant's safety rules."
        else:
            result.action = "review"
    for signal in result.signals:
        GUARDRAIL_HITS.labels(signal, result.action).inc()
    if result.action != "allow":
        log.warning("input_screened", score=result.score, signals=result.signals, action=result.action)
    return result


async def _judge(text: str, heuristic: ScreenResult) -> InjectionVerdict:
    from ordering.llm_client.client import get_llm_client
    from ordering.prompts import registry

    prompt = registry.get("injection_judge")
    system, user = prompt.render(text=text[:4000])
    fallback = InjectionVerdict(is_injection=heuristic.score >= 0.6, confidence=heuristic.score,
                                reason=f"heuristic signals: {', '.join(heuristic.signals)}")
    result = await get_llm_client().complete(prompt.route, system=system, user=user, schema=InjectionVerdict,
                                             fallback=fallback, prompt_id=prompt.prompt_id,
                                             prompt_version=prompt.version)
    return result.parsed


def sanitize_untrusted(text: str) -> tuple[str, list[str]]:
    """Drop instruction-like lines from retrieved/tool content before it enters a prompt."""
    kept, removed = [], []
    for line in text.split("\n"):
        if heuristic_score(line).score >= 0.45:
            removed.append(line.strip())
        else:
            kept.append(line)
    if removed:
        GUARDRAIL_HITS.labels("untrusted_content", "stripped").inc(len(removed))
    return "\n".join(kept).strip(), removed


def wrap_untrusted(tag: str, content: str) -> str:
    safe = re.sub(r"</?\s*" + re.escape(tag) + r"\s*>", "", content, flags=re.I)
    return f"<{tag}>\n{safe}\n</{tag}>"
