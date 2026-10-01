"""Route configuration: request type → ordered 3-model chain (one per provider).

Adapted from mcp-core ``mcp_servers/llm_wrapper/config.json`` (application/tenant/usecase →
model) - now YAML, versioned, with fallbacks, per-route timeouts, and tenant provider policy
(data residency) from ``governance/policy.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ordering.common.config import get_settings
from ordering.common.errors import ValidationFailed


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Route:
    name: str
    models: tuple[ModelTarget, ...]
    timeout_s: float
    max_tokens: int
    retries_per_model: int
    cacheable: bool = False


@dataclass(frozen=True)
class EmbeddingTarget:
    profile: str
    provider: str
    model: str
    dims: int


@dataclass(frozen=True)
class RoutingConfig:
    version: str
    routes: dict[str, Route]
    embeddings: dict[str, EmbeddingTarget]
    pricing: dict[str, dict[str, float]]

    def route(self, name: str) -> Route:
        if name not in self.routes:
            raise ValidationFailed(f"unknown route '{name}'", details={"routes": sorted(self.routes)})
        return self.routes[name]

    def all_models(self) -> list[ModelTarget]:
        seen, out = set(), []
        for r in self.routes.values():
            for m in r.models:
                if (m.provider, m.model) not in seen:
                    seen.add((m.provider, m.model))
                    out.append(m)
        return out


def parse_config(raw: dict[str, Any]) -> RoutingConfig:
    defaults = raw.get("defaults", {})
    routes = {}
    for name, spec in raw["routes"].items():
        models = tuple(
            ModelTarget(m["provider"], m["model"], {k: v for k, v in m.items() if k not in ("provider", "model")})
            for m in spec["models"]
        )
        providers = [m.provider for m in models]
        if len(models) != 3 or len(set(providers)) != 3:
            raise ValueError(f"route '{name}' must list exactly 3 models from 3 different providers")
        routes[name] = Route(
            name=name,
            models=models,
            timeout_s=float(spec.get("timeout_s", defaults.get("timeout_s", 30))),
            max_tokens=int(spec.get("max_tokens", defaults.get("max_tokens", 1024))),
            retries_per_model=int(spec.get("retries_per_model", defaults.get("retries_per_model", 1))),
            cacheable=bool(spec.get("cacheable", False)),
        )
    embeddings = {
        profile: EmbeddingTarget(profile, e["provider"], e["model"], int(e["dims"]))
        for profile, e in raw.get("embeddings", {}).items()
    }
    return RoutingConfig(str(raw.get("version", "unversioned")), routes, embeddings, raw.get("pricing", {}))


@lru_cache
def load_config(path: str | None = None) -> RoutingConfig:
    p = Path(path or get_settings().llm_routes_path)
    return parse_config(yaml.safe_load(p.read_text(encoding="utf-8")))


def cost_usd(config: RoutingConfig, model: str, input_tokens: int, output_tokens: int, cached_tokens: int) -> float:
    price = config.pricing.get(model)
    if not price:
        return 0.0
    uncached = max(input_tokens - cached_tokens, 0)
    return round(
        (uncached * price.get("input", 0) + cached_tokens * price.get("cached_input", 0)
         + output_tokens * price.get("output", 0)) / 1_000_000,
        6,
    )
