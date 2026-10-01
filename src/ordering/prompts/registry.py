"""Versioned prompt registry with canary rollouts.

Prompts live in ``prompts/library/*.yaml`` (id, owner, route, eval suite, versions). The version
used for a conversation is chosen by :func:`resolve` from the ``prompt_rollouts`` table
(traffic %, sticky per conversation via hashing) and falls back to ``default_version``. The
chosen ``prompt_id@version`` is stamped on the LLM request, ``llm_usage``, messages, feedback
and traces - closing the loop between production telemetry, evals, and prompt changes.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import text

from ordering.common.logging import get_logger

log = get_logger("ordering.prompts")
LIBRARY = Path(__file__).parent / "library"


@dataclass(frozen=True)
class PromptVersion:
    prompt_id: str
    version: str
    route: str
    system: str
    user: str

    @property
    def ref(self) -> str:
        return f"{self.prompt_id}@{self.version}"

    def render(self, **values) -> tuple[str, str]:
        return self.system.strip(), self.user.format(**values).strip()


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    route: str
    default_version: str
    versions: dict[str, PromptVersion]
    owner: str | None = None
    eval_suite: str | None = None


@lru_cache
def load_library(path: str | None = None) -> dict[str, PromptSpec]:
    specs = {}
    for file in sorted(Path(path or LIBRARY).glob("*.yaml")):
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
        versions = {
            v["version"]: PromptVersion(raw["id"], v["version"], raw["route"], v["system"], v["user"])
            for v in raw["versions"]
        }
        if raw["default_version"] not in versions:
            raise ValueError(f"{file.name}: default_version not found")
        specs[raw["id"]] = PromptSpec(raw["id"], raw["route"], raw["default_version"], versions, raw.get("owner"),
                                      raw.get("eval_suite"))
    return specs


_rollout_cache: dict[str, tuple[float, list[tuple[str, int]]]] = {}
_ROLLOUT_TTL_S = 30


async def _rollouts(prompt_id: str) -> list[tuple[str, int]]:
    cached = _rollout_cache.get(prompt_id)
    if cached and time.monotonic() - cached[0] < _ROLLOUT_TTL_S:
        return cached[1]
    rows: list[tuple[str, int]] = []
    try:
        from ordering.common.db import get_sessionmaker

        async with get_sessionmaker()() as s:
            result = await s.execute(
                text("SELECT version, traffic_pct FROM prompt_rollouts WHERE prompt_id = :p AND is_active "
                     "ORDER BY version"), {"p": prompt_id})
            rows = [(r.version, r.traffic_pct) for r in result]
    except Exception as exc:  # DB unavailable → default version
        log.debug("rollout_lookup_failed", error=str(exc)[:120])
    _rollout_cache[prompt_id] = (time.monotonic(), rows)
    return rows


def pick_version(spec: PromptSpec, rollouts: list[tuple[str, int]], sticky_key: str) -> str:
    bucket = int(hashlib.sha256(f"{spec.prompt_id}:{sticky_key}".encode()).hexdigest(), 16) % 100
    cumulative = 0
    for version, pct in rollouts:
        cumulative += pct
        if bucket < cumulative and version in spec.versions:
            return version
    return spec.default_version


async def resolve(prompt_id: str, sticky_key: str | None = None, version: str | None = None) -> PromptVersion:
    spec = load_library()[prompt_id]
    if version is None:
        version = pick_version(spec, await _rollouts(prompt_id), sticky_key or "default")
    return spec.versions[version]


def get(prompt_id: str, version: str | None = None) -> PromptVersion:
    spec = load_library()[prompt_id]
    return spec.versions[version or spec.default_version]
