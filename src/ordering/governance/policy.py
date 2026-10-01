"""Loads ``governance/policy.yaml`` and exposes typed accessors used across services."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ordering.common.config import get_settings


class Autonomy(BaseModel):
    max_investigation_steps: int = 4
    human_approval_required: list[str] = Field(default_factory=list)
    approval_ttl_minutes: int = 30
    high_value_requires_role: str = "approver"


class Pii(BaseModel):
    redact_classes: list[str] = Field(default_factory=list)
    store_pii_in_memory: bool = False


class Content(BaseModel):
    blocked_categories: list[str] = Field(default_factory=list)
    require_citations_for_policy_answers: bool = True
    ai_disclosure: str = "AI-generated response."


class MemoryPolicy(BaseModel):
    retention_days: int = 180
    allow_types: list[str] = Field(default_factory=lambda: ["preference", "fact", "procedural"])
    deny_patterns: list[str] = Field(default_factory=list)


class InjectionPolicy(BaseModel):
    block_threshold: float = 0.8
    review_threshold: float = 0.45


class Policy(BaseModel):
    version: str
    owner: str | None = None
    autonomy: Autonomy = Field(default_factory=Autonomy)
    pii: Pii = Field(default_factory=Pii)
    content: Content = Field(default_factory=Content)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    injection: InjectionPolicy = Field(default_factory=InjectionPolicy)
    tenants: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def allowed_providers(self, tenant: str | None) -> set[str]:
        spec = self.tenants.get(tenant or "", self.tenants.get("default", {}))
        return set(spec.get("allowed_providers", ["anthropic", "openai", "gemini"]))


@lru_cache
def get_policy() -> Policy:
    path = get_settings().governance_policy_path
    return Policy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
