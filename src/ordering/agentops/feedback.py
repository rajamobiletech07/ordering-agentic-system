"""Feedback capture and mining (steps 1-3 of the continuous feedback loop).

1. **Capture** - explicit user feedback (``POST /v1/conversations/{id}/feedback``) and implicit
   signals (guardrail blocks, insufficient-evidence refusals, degraded LLM responses, tool
   errors, rejected approvals) are linked to message, trace, prompt version and agent version.
2. **Mine** - low ratings and signals become ``eval_candidates`` (redacted) automatically.
3. **Curate** - ``export`` writes candidates to JSONL for review; ``promote`` appends a reviewed
   case to a versioned eval dataset under ``evals/datasets/``.

    python -m ordering.agentops.feedback export --out reports/eval_candidates.jsonl
    python -m ordering.agentops.feedback promote <candidate_id> --suite intent --expected-intent policy_question
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import typer
from sqlalchemy import text

from ordering.common.config import PROJECT_ROOT
from ordering.common.context import Principal
from ordering.common.db import admin_session, dispose_engines, tenant_session
from ordering.common.logging import get_logger
from ordering.common.metrics import FEEDBACK
from ordering.common.redaction import redact_text

log = get_logger("ordering.agentops.feedback")
cli = typer.Typer(add_completion=False, help="Feedback loop: export and promote eval candidates")


async def record_feedback(principal: Principal, conversation_id: UUID, message: dict | None, rating: int,
                          labels: list[str], comment: str | None, user_message: str | None) -> UUID:
    fid = uuid4()
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        await s.execute(text(
            """INSERT INTO feedback (feedback_id, tenant_id, user_id, conversation_id, message_id, rating, labels, comment,
                   trace_id, prompt_version, agent_version)
               VALUES (:f, :t, :u, :c, :m, :r, :l, :cm, :tr, :pv, :av)"""),
            {"f": fid, "t": principal.tenant_id, "u": principal.user_id, "c": conversation_id,
             "m": message["message_id"] if message else None, "r": rating, "l": labels,
             "cm": redact_text(comment) if comment else None, "tr": message.get("trace_id") if message else None,
             "pv": message.get("prompt_version") if message else None,
             "av": message.get("agent_version") if message else None})
        if rating <= 2 or any(label in labels for label in ("hallucination", "wrong_recommendation", "unsafe",
                                                             "wrong_order_details", "bad_citation")):
            await s.execute(text(
                """INSERT INTO eval_candidates (candidate_id, tenant_id, source, conversation_id, message_id, trace_id,
                       input_text, output_text, labels)
                   VALUES (:id, :t, 'feedback', :c, :m, :tr, :i, :o, :l) ON CONFLICT DO NOTHING"""),
                {"id": uuid4(), "t": principal.tenant_id, "c": conversation_id,
                 "m": message["message_id"] if message else None, "tr": message.get("trace_id") if message else None,
                 "i": redact_text(user_message or "")[:4000] or "(unknown)",
                 "o": redact_text(message["content"])[:4000] if message else None, "l": labels})
    FEEDBACK.labels(str(rating)).inc()
    return fid


async def record_signal(principal: Principal, source: str, conversation_id: str | None, message_id: UUID | None,
                        trace_id: str | None, input_text: str, output_text: str | None, labels: list[str]) -> None:
    """Implicit production signal → eval candidate (best-effort)."""
    try:
        async with tenant_session(principal.tenant_id, principal.user_id) as s:
            await s.execute(text(
                """INSERT INTO eval_candidates (candidate_id, tenant_id, source, conversation_id, message_id, trace_id,
                       input_text, output_text, labels)
                   VALUES (:id, :t, :src, :c, :m, :tr, :i, :o, :l) ON CONFLICT DO NOTHING"""),
                {"id": uuid4(), "t": principal.tenant_id, "src": source, "c": conversation_id, "m": message_id,
                 "tr": trace_id, "i": redact_text(input_text)[:4000], "o": redact_text(output_text or "")[:4000],
                 "l": labels})
    except Exception as exc:
        log.warning("signal_record_failed", error=str(exc)[:120])


async def export_candidates(out: Path, status: str = "new", limit: int = 500) -> int:
    async with admin_session() as s:
        rows = (await s.execute(text(
            """SELECT candidate_id, tenant_id, source, trace_id, input_text, output_text, labels, created_at
               FROM eval_candidates WHERE status = :st ORDER BY created_at DESC LIMIT :l"""),
            {"st": status, "l": limit})).all()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({**dict(r._mapping), "candidate_id": str(r.candidate_id)}, default=str) + "\n")
    return len(rows)


async def promote(candidate_id: UUID, suite: str, expected: dict) -> Path:
    async with admin_session() as s:
        row = (await s.execute(text("SELECT * FROM eval_candidates WHERE candidate_id = :c"), {"c": candidate_id})).one()
        await s.execute(text("UPDATE eval_candidates SET status = 'promoted', suite = :s WHERE candidate_id = :c"),
                        {"s": suite, "c": candidate_id})
    dataset = PROJECT_ROOT / "evals" / "datasets" / f"{suite}.jsonl"
    case = {"id": f"fb-{str(candidate_id)[:8]}", "tenant": row.tenant_id, "input": row.input_text,
            "source": f"feedback:{row.source}", **expected}
    with dataset.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(case) + "\n")
    return dataset


def _run(coro):
    async def wrapper():
        try:
            return await coro
        finally:
            await dispose_engines()

    return asyncio.run(wrapper())


@cli.command("export")
def export_cmd(out: Path = typer.Option(Path("reports/eval_candidates.jsonl")), status: str = "new") -> None:
    count = _run(export_candidates(out, status))
    typer.echo(json.dumps({"exported": count, "file": str(out)}))


@cli.command("promote")
def promote_cmd(candidate_id: str, suite: str = typer.Option(...), expected_intent: str = typer.Option(None),
                expect_insufficient: bool = typer.Option(False)) -> None:
    expected: dict = {}
    if expected_intent:
        expected["expected_intent"] = expected_intent
    if expect_insufficient:
        expected["expect_insufficient"] = True
    path = _run(promote(UUID(candidate_id), suite, expected))
    typer.echo(json.dumps({"promoted": candidate_id, "dataset": str(path)}))


if __name__ == "__main__":
    cli()
