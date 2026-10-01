"""Eval runner with baseline regression gating (used locally, in CI, and by the nightly feedback loop).

    python -m ordering.agentops.evals.runner run --suite all
    python -m ordering.agentops.evals.runner run --suite intent --prompt-version 1.0.0
    python -m ordering.agentops.evals.runner run --suite all --update-baseline

Each run writes ``reports/evals/<ts>-<suite>.json`` + a Markdown summary, records an
``eval_runs`` row (best-effort), exports scores as Prometheus metrics in-process, and exits non-zero
when any metric regresses more than ``tolerance`` below ``evals/baselines/<suite>.json``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import typer
from sqlalchemy import text

from ordering.agentops.evals.scorers import SUITES
from ordering.common.config import PROJECT_ROOT, get_settings
from ordering.common.db import admin_session, dispose_engines
from ordering.common.http import close_http_client
from ordering.common.logging import configure_logging
from ordering.common.metrics import EVAL_SCORE

cli = typer.Typer(add_completion=False, help="Agent evaluation suites")


@cli.callback()
def _root() -> None:
    """Agent evaluation suites (keeps `run` as an explicit sub-command)."""
BASELINES = PROJECT_ROOT / "evals" / "baselines"
REPORTS = PROJECT_ROOT / "reports" / "evals"


def compare(metrics: dict[str, float], baseline: dict[str, float], tolerance: float) -> list[str]:
    return [f"{k}: {metrics.get(k)} < baseline {v} - {tolerance}" for k, v in baseline.items()
            if k in metrics and not k.startswith("p95") and metrics[k] < v - tolerance]


async def run_suite(name: str, prompt_version: str | None, tolerance: float, update_baseline: bool) -> dict:
    result = await SUITES[name](prompt_version=prompt_version)
    baseline_path = BASELINES / f"{name}.json"
    baseline = json.loads(baseline_path.read_text())["metrics"] if baseline_path.exists() else {}
    regressions = compare(result["metrics"], baseline, tolerance)
    result.update({"suite": name, "baseline": baseline, "regressions": regressions, "passed": not regressions,
                   "agent_version": get_settings().agent_version, "timestamp": datetime.now(UTC).isoformat()})
    for metric, value in result["metrics"].items():
        EVAL_SCORE.labels(name, metric).observe(value)
    if update_baseline:
        baseline_path.write_text(json.dumps({"metrics": result["metrics"], "updated": result["timestamp"]}, indent=2) + "\n")
    try:
        async with admin_session() as s:
            await s.execute(text(
                """INSERT INTO eval_runs (run_id, suite, prompt_versions, agent_version, metrics, baseline, passed, report_path)
                   VALUES (:id, :s, CAST(:pv AS JSONB), :av, CAST(:m AS JSONB), CAST(:b AS JSONB), :p, :r)"""),
                {"id": uuid.uuid4(), "s": name, "pv": json.dumps({"override": prompt_version, **result.get("info", {})}),
                 "av": result["agent_version"], "m": json.dumps(result["metrics"]), "b": json.dumps(baseline),
                 "p": result["passed"], "r": str(REPORTS)})
    except Exception as exc:  # DB optional for offline runs
        result["info"]["eval_runs_write"] = f"skipped: {exc.__class__.__name__}"
    return result


def write_reports(results: list[dict]) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = REPORTS / f"{stamp}-evals.json"
    path.write_text(json.dumps(results, indent=2, default=str))
    lines = ["# Eval report", "", f"_{stamp} · agent {get_settings().agent_version}_", "",
             "| suite | metric | value | baseline | status |", "|---|---|---|---|---|"]
    for r in results:
        for metric, value in r["metrics"].items():
            base = r["baseline"].get(metric, "-")
            bad = any(x.startswith(f"{metric}:") for x in r["regressions"])
            lines.append(f"| {r['suite']} | {metric} | {value} | {base} | {'❌ regression' if bad else '✅'} |")
    for r in results:
        if r["failures"]:
            lines += ["", f"## {r['suite']} failures ({len(r['failures'])})", ""]
            lines += [f"- `{json.dumps(f, default=str)[:300]}`" for f in r["failures"][:20]]
    (REPORTS / f"{stamp}-evals.md").write_text("\n".join(lines) + "\n")
    return path


@cli.command()
def run(suite: str = typer.Option("all"), prompt_version: str = typer.Option(None), tolerance: float = 0.02,
        update_baseline: bool = typer.Option(False)) -> None:
    configure_logging("evals", "WARNING")
    names = list(SUITES) if suite == "all" else [s.strip() for s in suite.split(",")]

    async def main():
        try:
            return [await run_suite(n, prompt_version, tolerance, update_baseline) for n in names]
        finally:
            await close_http_client()
            await dispose_engines()

    results = asyncio.run(main())
    path = write_reports(results)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        typer.echo(f"[{status}] {r['suite']:<13} {json.dumps(r['metrics'])}")
        for reg in r["regressions"]:
            typer.echo(f"        regression: {reg}")
    typer.echo(f"report: {path}")
    if not all(r["passed"] for r in results):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    cli()
