"""Retrieval / answer evaluation.

Dataset rows (JSONL): ``{"id", "tenant", "query", "expected_sources": [source_uri...],
"expect_insufficient": bool, "must_not_sources": [...], "answer_contains": [...]}``

Metrics: recall@k, MRR, refusal accuracy (insufficient-evidence handling), tenant leakage
(retrieving another tenant's document = hard failure), and answer keyword coverage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ordering.rag.answerer import answer_question
from ordering.rag.retriever import retrieve


@dataclass
class EvalReport:
    total: int = 0
    recall_at_k: float = 0.0
    mrr: float = 0.0
    refusal_accuracy: float = 0.0
    answer_coverage: float = 0.0
    tenant_leaks: int = 0
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__


def load_dataset(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def evaluate(path: Path, k: int = 5, with_answers: bool = True) -> EvalReport:
    rows = load_dataset(path)
    report = EvalReport(total=len(rows))
    hits = rr = refusal_ok = refusal_total = coverage_hits = coverage_total = 0
    answerable = 0
    for row in rows:
        result = await retrieve(row["query"], row["tenant"], top_k=k)
        uris = [c.source_uri for c in result.chunks]
        leaked = [c.source_uri for c in result.chunks if c.tenant_id not in (None, row["tenant"])]
        forbidden = [u for u in uris if u in row.get("must_not_sources", [])]
        if leaked or forbidden:
            report.tenant_leaks += 1
            report.failures.append({"id": row["id"], "error": "tenant_leak", "sources": leaked + forbidden})
        expect_insufficient = row.get("expect_insufficient", False)
        answer = await answer_question(row["query"], row["tenant"]) if (with_answers or expect_insufficient) else None
        if expect_insufficient:
            refusal_total += 1
            if answer is not None and not answer.sufficient:
                refusal_ok += 1
            else:
                report.failures.append({"id": row["id"], "error": "should_refuse", "top": uris[:2]})
            continue
        answerable += 1
        expected = set(row.get("expected_sources", []))
        rank = next((i for i, u in enumerate(uris, start=1) if u in expected), None)
        if rank:
            hits += 1
            rr += 1.0 / rank
        else:
            report.failures.append({"id": row["id"], "error": "missed", "expected": sorted(expected), "got": uris[:3]})
        if answer is not None and answer.sufficient is False:
            report.failures.append({"id": row["id"], "error": "false_refusal"})
        if answer is not None and row.get("answer_contains"):
            coverage_total += 1
            if all(s.lower() in answer.answer.lower() for s in row["answer_contains"]):
                coverage_hits += 1
            else:
                report.failures.append({"id": row["id"], "error": "answer_missing_terms",
                                        "expected_terms": row["answer_contains"], "answer": answer.answer[:200]})
    report.recall_at_k = round(hits / answerable, 3) if answerable else 1.0
    report.mrr = round(rr / answerable, 3) if answerable else 1.0
    report.refusal_accuracy = round(refusal_ok / refusal_total, 3) if refusal_total else 1.0
    report.answer_coverage = round(coverage_hits / coverage_total, 3) if coverage_total else 1.0
    return report
