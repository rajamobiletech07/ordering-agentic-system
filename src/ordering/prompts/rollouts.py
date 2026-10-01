"""Canary / A-B rollouts for prompt versions (step 5 of the feedback loop).

    python -m ordering.prompts.rollouts list
    python -m ordering.prompts.rollouts set intent_classifier 1.0.0 20     # 20% of conversations
    python -m ordering.prompts.rollouts clear intent_classifier            # instant rollback to default

Assignment is sticky per conversation (hash of prompt id + conversation id), so a thread never
switches prompt versions mid-way. Agents pick up changes within ~30 s (rollout cache TTL).
"""

from __future__ import annotations

import asyncio
import json

import typer
from sqlalchemy import text

from ordering.common.db import admin_session, dispose_engines
from ordering.prompts.registry import load_library

cli = typer.Typer(add_completion=False, help="Prompt version rollouts")


def _run(coro):
    async def wrapper():
        try:
            return await coro
        finally:
            await dispose_engines()

    return asyncio.run(wrapper())


@cli.command("list")
def list_cmd() -> None:
    async def main():
        async with admin_session() as s:
            return [dict(r._mapping) for r in await s.execute(text("SELECT * FROM prompt_rollouts ORDER BY prompt_id, version"))]

    rows = _run(main())
    for spec in load_library().values():
        active = [f"{r['version']}={r['traffic_pct']}%" for r in rows if r["prompt_id"] == spec.prompt_id and r["is_active"]]
        typer.echo(f"{spec.prompt_id:<26} default={spec.default_version:<7} versions={sorted(spec.versions)} "
                   f"rollouts={active or '-'}")


@cli.command("set")
def set_cmd(prompt_id: str, version: str, traffic_pct: int = typer.Argument(..., min=0, max=100)) -> None:
    spec = load_library().get(prompt_id)
    if spec is None or version not in spec.versions:
        raise typer.BadParameter(f"unknown prompt/version {prompt_id}@{version}")

    async def main():
        async with admin_session() as s:
            await s.execute(text(
                """INSERT INTO prompt_rollouts (prompt_id, version, traffic_pct, is_active) VALUES (:p, :v, :t, true)
                   ON CONFLICT (prompt_id, version) DO UPDATE SET traffic_pct = :t, is_active = true"""),
                {"p": prompt_id, "v": version, "t": traffic_pct})
            total = (await s.execute(text(
                "SELECT COALESCE(sum(traffic_pct), 0) FROM prompt_rollouts WHERE prompt_id = :p AND is_active"),
                {"p": prompt_id})).scalar_one()
            if total > 100:
                raise typer.BadParameter(f"active rollouts for {prompt_id} exceed 100% ({total}%)")

    _run(main())
    typer.echo(json.dumps({"prompt_id": prompt_id, "version": version, "traffic_pct": traffic_pct}))


@cli.command("clear")
def clear_cmd(prompt_id: str) -> None:
    async def main():
        async with admin_session() as s:
            await s.execute(text("UPDATE prompt_rollouts SET is_active = false WHERE prompt_id = :p"), {"p": prompt_id})

    _run(main())
    typer.echo(json.dumps({"prompt_id": prompt_id, "rollouts": "cleared (default version serves 100%)"}))


if __name__ == "__main__":
    cli()
