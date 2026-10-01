"""Eval suites and scorers (step 4 of the feedback loop).

Suites:
  intent        - intent accuracy + supervisor route accuracy (the agent's tool plan entry point)
  injection     - guardrail block recall and false-positive rate
  rag           - recall@k, MRR, refusal accuracy, tenant leakage, answer coverage
  groundedness  - LLM-as-judge (``eval_judge`` route) with a deterministic lexical-support fallback,
                  plus citation validity
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ordering.common.config import PROJECT_ROOT
from ordering.common.context import Principal, RequestContext, set_context

DATASETS = PROJECT_ROOT / "evals" / "datasets"
EVALUATOR = Principal(user_id="system:evals", tenant_id="acme-retail", roles=frozenset({"viewer"}))


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def intent_suite(prompt_version: str | None = None) -> dict[str, Any]:
    from ordering.agents.supervisor import nodes
    from ordering.agents.supervisor.intents import Intent, classify_rules
    from ordering.llm_client.client import get_llm_client
    from ordering.memory.short_term import state_brief
    from ordering.prompts import registry

    set_context(RequestContext(principal=EVALUATOR, agent="supervisor"))
    prompt = await registry.resolve("intent_classifier", version=prompt_version)
    rows = load(DATASETS / "intent.jsonl")
    correct_intent = correct_route = degraded = 0
    failures, latencies = [], []
    for row in rows:
        st = row.get("state", {})
        state = {"last_recommendations": [{"sku": "SKU-10042", "location_code": "STORE-014"}] if st.get("has_recommendations") else [],
                 "pending_approval": {"action": "create_order", "summary": "pending"} if st.get("pending_approval") else None}
        fallback = classify_rules(row["input"], bool(state["pending_approval"]), bool(state["last_recommendations"]))
        system, user = prompt.render(state=state_brief(state), message=row["input"])
        started = time.perf_counter()
        result = await get_llm_client().complete(prompt.route, system=system, user=user, schema=Intent, fallback=fallback,
                                                 prompt_id=prompt.prompt_id, prompt_version=prompt.version)
        latencies.append(time.perf_counter() - started)
        intent = result.parsed.intent
        if intent in ("confirm", "reject") and not state["pending_approval"]:
            intent = "unsupported"
        degraded += int(result.degraded)
        route = nodes.route_after_classify({"intent": {"intent": intent}})
        ok_i, ok_r = intent == row["expected_intent"], route == row["expected_route"]
        correct_intent += ok_i
        correct_route += ok_r
        if not (ok_i and ok_r):
            failures.append({"id": row["id"], "input": row["input"], "expected": row["expected_intent"], "got": intent})
    n = len(rows)
    return {"metrics": {"intent_accuracy": round(correct_intent / n, 3), "route_accuracy": round(correct_route / n, 3),
                        "p95_latency_s": round(sorted(latencies)[int(0.95 * (n - 1))], 3)},
            "info": {"cases": n, "degraded_llm_calls": degraded, "prompt": prompt.ref}, "failures": failures}


async def injection_suite(**_) -> dict[str, Any]:
    from ordering.agents.guardrails import screen_input

    set_context(RequestContext(principal=EVALUATOR, agent="supervisor"))
    rows = load(DATASETS / "injection.jsonl")
    tp = fn = fp = tn = 0
    failures = []
    for row in rows:
        action = (await screen_input(row["input"])).action
        blocked = action == "block"
        if row["expected"] == "block":
            tp, fn = tp + blocked, fn + (not blocked)
        else:
            fp, tn = fp + blocked, tn + (not blocked)
        if blocked != (row["expected"] == "block"):
            failures.append({"id": row["id"], "input": row["input"], "expected": row["expected"], "got": action})
    return {"metrics": {"block_recall": round(tp / max(tp + fn, 1), 3),
                        "allow_accuracy": round(tn / max(tn + fp, 1), 3)},
            "info": {"cases": len(rows)}, "failures": failures}


async def rag_suite(**_) -> dict[str, Any]:
    from ordering.rag.eval import evaluate

    report = await evaluate(PROJECT_ROOT / "data" / "rag_eval" / "eval_set.jsonl", k=5)
    return {"metrics": {"recall_at_5": report.recall_at_k, "mrr": report.mrr, "refusal_accuracy": report.refusal_accuracy,
                        "answer_coverage": report.answer_coverage, "tenant_isolation": 1.0 if report.tenant_leaks == 0 else 0.0},
            "info": {"cases": report.total}, "failures": report.failures}


class GroundednessVerdict(BaseModel):
    score: float = Field(..., ge=0, le=1)
    unsupported_claims: list[str] = Field(default_factory=list)


def lexical_support(answer: str, sources: list[str]) -> float:
    """Deterministic judge: share of answer sentences whose content words mostly appear in the sources."""
    source_words = set(re.findall(r"[a-z0-9]{4,}", " ".join(sources).lower()))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\[\d+\]", "", answer)) if len(s.split()) > 3]
    if not sentences:
        return 0.0
    supported = 0
    for sentence in sentences:
        words = set(re.findall(r"[a-z0-9]{4,}", sentence.lower())) - {"according", "policy", "version"}
        if words and len(words & source_words) / len(words) >= 0.6:
            supported += 1
    return supported / len(sentences)


async def groundedness_suite(**_) -> dict[str, Any]:
    from ordering.llm_client.client import get_llm_client
    from ordering.prompts import registry
    from ordering.rag.answerer import answer_question

    set_context(RequestContext(principal=EVALUATOR, agent="evaluator"))
    judge = registry.get("groundedness_judge")
    scores, cited, failures = [], 0, []
    rows = load(DATASETS / "groundedness.jsonl")
    for row in rows:
        result = await answer_question(row["input"], row["tenant"])
        sources = [c.content for c in (result.retrieval.chunks if result.retrieval else [])]
        if not result.sufficient:
            cited += 1  # an explicit refusal is a valid, grounded outcome
            scores.append(1.0)
            continue
        cited += int(bool(result.citations))
        fallback = GroundednessVerdict(score=round(lexical_support(result.answer, sources), 3))
        system, user = judge.render(question=row["input"], answer=result.answer, sources="\n\n".join(sources)[:8000])
        verdict = (await get_llm_client().complete(judge.route, system=system, user=user, schema=GroundednessVerdict,
                                                   fallback=fallback, prompt_id=judge.prompt_id,
                                                   prompt_version=judge.version)).parsed
        scores.append(verdict.score)
        if verdict.score < 0.7:
            failures.append({"id": row["id"], "answer": result.answer[:300], "score": verdict.score,
                             "unsupported": verdict.unsupported_claims})
    return {"metrics": {"groundedness": round(sum(scores) / len(scores), 3), "citation_validity": round(cited / len(rows), 3)},
            "info": {"cases": len(rows)}, "failures": failures}


SUITES = {"intent": intent_suite, "injection": injection_suite, "rag": rag_suite, "groundedness": groundedness_suite}
