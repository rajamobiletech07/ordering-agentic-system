"""RAG command line.

    python -m ordering.rag.cli ingest --path data/rag_docs --profile openai|gemini|local|hashing|all
    python -m ordering.rag.cli search "what is the approval threshold" --tenant acme-retail
    python -m ordering.rag.cli ask "what happens when a payment fails?" --tenant acme-retail
    python -m ordering.rag.cli eval --set data/rag_eval/eval_set.jsonl --min-recall 0.8
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from ordering.common.config import get_settings
from ordering.common.db import dispose_engines
from ordering.common.http import close_http_client
from ordering.common.logging import configure_logging

app = typer.Typer(add_completion=False, help="RAG ingestion, search and evaluation")
PROFILES = ["openai", "gemini", "local", "hashing"]


def _run(coro):
    async def wrapper():
        try:
            return await coro
        finally:
            await close_http_client()
            await dispose_engines()

    return asyncio.run(wrapper())


@app.command()
def ingest(path: Path = typer.Option(Path("data/rag_docs")),
           profile: str = typer.Option(None, help="openai|gemini|local|hashing, comma-separated, or 'all'")) -> None:
    """Ingest (or re-ingest) every document under PATH. Idempotent; back-fills missing embeddings."""
    from ordering.rag.ingest import ingest_path

    configure_logging("rag-cli", "WARNING")
    selected = profile or get_settings().embedding_profile
    profiles = PROFILES if selected == "all" else [p.strip() for p in selected.split(",") if p.strip()]
    unknown = set(profiles) - set(PROFILES)
    if unknown:
        raise typer.BadParameter(f"unknown profile(s): {sorted(unknown)}")
    results = _run(ingest_path(path, profiles))
    summary: dict[str, int] = {}
    for r in results:
        summary[r.status] = summary.get(r.status, 0) + 1
        extra = f" duplicate_of={r.duplicate_of}" if r.duplicate_of else ""
        typer.echo(f"{r.status:<18} {(r.tenant_id or 'shared'):<15} {r.source_uri:<45} v{r.version_no or '-'} "
                   f"chunks={r.chunks} embedded={r.embedded}{extra}")
    typer.echo(json.dumps({"summary": summary, "profiles": profiles}))


@app.command()
def search(query: str, tenant: str = typer.Option(...), k: int = 5, profile: str = typer.Option(None)) -> None:
    from ordering.rag.retriever import retrieve

    configure_logging("rag-cli", "WARNING")
    result = _run(retrieve(query, tenant, top_k=k, profile=profile))
    typer.echo(f"profile={result.profile} reranked={result.reranked} degraded={result.degraded} sufficient={result.sufficient}")
    for c in result.chunks:
        typer.echo(f"{c.score:.3f}  vec={c.vector_score:.3f} kw={c.keyword_score:.3f}  {c.source_uri} :: {c.section_path}")


@app.command()
def ask(question: str, tenant: str = typer.Option(...)) -> None:
    from ordering.rag.answerer import answer_question

    configure_logging("rag-cli", "WARNING")
    result = _run(answer_question(question, tenant))
    typer.echo(result.answer)
    for c in result.citations:
        typer.echo(f"  [{c.ref}] {c.document} v{c.version} - {c.section} ({c.source_uri})")


@app.command("eval")
def eval_cmd(
    set_path: Path = typer.Option(Path("data/rag_eval/eval_set.jsonl"), "--set"),
    k: int = 5,
    min_recall: float = 0.8,
    min_refusal: float = 0.8,
    out: Path = typer.Option(None),
) -> None:
    """Run retrieval/answer evals; exits non-zero when below thresholds or on any tenant leak."""
    from ordering.rag.eval import evaluate

    configure_logging("rag-cli", "WARNING")
    report = _run(evaluate(set_path, k))
    payload = json.dumps(report.to_dict(), indent=2)
    typer.echo(payload)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload)
    if report.recall_at_k < min_recall or report.refusal_accuracy < min_refusal or report.tenant_leaks:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
