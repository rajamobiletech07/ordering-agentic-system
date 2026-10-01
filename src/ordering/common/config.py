"""Environment-based configuration.

Adapted from mcp-core ``inventory_smart_server/configuration/environment.py`` (singleton
environment object) but rewritten on pydantic-settings:

* values come from environment variables / ``.env`` only - never hard-coded keys;
* any secret can instead be supplied as a file via ``<NAME>_FILE`` (Docker/K8s secrets
  mounts, Vault agent sidecars), which keeps secrets out of ``docker inspect`` output;
* secrets are ``SecretStr`` so they never appear in logs or reprs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_SECRET_FIELDS = (
    "jwt_secret",
    "service_jwt_secret",
    "anthropic_api_key",
    "openai_api_key",
    "google_api_key",
    "litellm_api_key",
    "database_url",
    "database_admin_url",
    "redis_url",  # carries the AUTH password on managed Redis (Memorystore)
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- runtime -------------------------------------------------------------------
    env: Literal["local", "test", "staging", "production"] = "local"
    service_name: str = "ordering"
    agent_version: str = "1.0.0"
    log_level: str = "INFO"

    # --- data stores ---------------------------------------------------------------
    # Services connect as the non-owner ``app_user`` so Postgres RLS applies.
    database_url: SecretStr = SecretStr("postgresql+psycopg://app_user:app_password@localhost:55432/ordering")
    # Owner role - migrations and seeding only.
    database_admin_url: SecretStr = SecretStr("postgresql+psycopg://ordering:ordering@localhost:55432/ordering")
    db_pool_size: int = 10
    db_statement_timeout_ms: int = 15000
    redis_url: str = "redis://localhost:6379/0"

    # --- auth ------------------------------------------------------------------------
    jwt_secret: SecretStr = SecretStr("dev-only-change-me-user-token-secret-0123456789")
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "ordering-idp"
    jwt_audience: str = "ordering-api"
    jwt_jwks_url: str | None = None  # production: OIDC IdP JWKS endpoint (RS256)
    service_jwt_secret: SecretStr = SecretStr("dev-only-change-me-service-token-secret-9876543210")
    service_jwt_audience: str = "ordering-mcp"
    service_token_ttl_s: int = 120
    enable_dev_token_endpoint: bool = True

    # --- service topology ------------------------------------------------------------------
    agent_service_url: str = "http://localhost:8010"
    llm_gateway_url: str = "http://localhost:8020"
    mcp_recommendation_url: str = "http://localhost:9001/mcp"
    mcp_ordering_url: str = "http://localhost:9002/mcp"
    mcp_payment_url: str = "http://localhost:9003/mcp"
    mcp_notification_url: str = "http://localhost:9004/mcp"

    # --- LLM gateway ---------------------------------------------------------------
    llm_backend: Literal["native", "litellm"] = "native"
    llm_provider_override: Literal["", "mock"] = ""
    llm_routes_path: Path = PROJECT_ROOT / "src/ordering/llm_gateway/config/routes.yaml"
    litellm_url: str = "http://localhost:4000"
    litellm_api_key: SecretStr = SecretStr("sk-litellm-dev-master-key")
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    llm_request_timeout_s: float = 45.0
    llm_route_deadline_s: float = 90.0
    tenant_daily_budget_usd: float = 25.0

    # --- RAG ---------------------------------------------------------------------------
    embedding_profile: Literal["openai", "gemini", "local", "hashing"] = "openai"
    reranker_enabled: bool = True
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    local_embedding_model: str = "BAAI/bge-small-en-v1.5"
    rag_min_rerank_score: float = 0.2
    rag_min_fused_score: float = 0.02
    rag_top_k: int = 5
    fastembed_cache_dir: str = str(PROJECT_ROOT / ".cache/fastembed")

    # --- API gateway -----------------------------------------------------------------
    rate_limit_per_minute: int = 60
    max_body_bytes: int = 64 * 1024
    agent_request_timeout_s: float = 60.0

    # --- memory & cache ------------------------------------------------------------------
    thread_cache_ttl_s: int = 1800
    cache_sweep_interval_s: int = 300
    short_term_window_turns: int = 8
    memory_retention_days: int = 180
    long_term_memory_enabled: bool = True

    # --- workflow ------------------------------------------------------------------------
    approval_ttl_minutes: int = 30
    # Create LangGraph checkpoint tables at agent-service startup (local/compose). Kubernetes sets
    # false: the db-migrate job runs ``scripts.setup_checkpointer`` with the owner role instead.
    checkpointer_auto_setup: bool = True
    governance_policy_path: Path = PROJECT_ROOT / "governance/policy.yaml"
    prompts_dir: Path = PROJECT_ROOT / "src/ordering/prompts"

    # --- resilience / testing ----------------------------------------------------------
    # Comma separated fault injection flags, honoured only outside production,
    # e.g. "payment:unavailable,notification:flaky".
    fault_injection: str = ""

    # --- observability -----------------------------------------------------------------
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    metrics_enabled: bool = True

    notification_worker_poll_s: float = 1.0
    notification_max_attempts: int = 5

    @model_validator(mode="before")
    @classmethod
    def _load_secret_files(cls, values: Any) -> Any:
        """Support ``<NAME>_FILE`` for every secret field (Docker / K8s secrets)."""
        if not isinstance(values, dict):
            return values
        for field in _SECRET_FIELDS:
            path = os.environ.get(f"{field.upper()}_FILE")
            if path and Path(path).is_file():
                values[field] = Path(path).read_text(encoding="utf-8").strip()
        return values

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        if self.env == "production":
            for name in ("jwt_secret", "service_jwt_secret"):
                if getattr(self, name).get_secret_value().startswith("dev-only"):
                    raise ValueError(f"{name} must be set to a real secret in production")
            if self.enable_dev_token_endpoint:
                raise ValueError("enable_dev_token_endpoint must be false in production")
        return self

    def faults(self) -> set[str]:
        if self.env == "production":
            return set()
        return {f.strip() for f in self.fault_injection.split(",") if f.strip()}

    def secret(self, name: str) -> str | None:
        value = getattr(self, name)
        return value.get_secret_value() if value is not None else None

    def require_secret(self, name: str) -> str:
        value = self.secret(name)
        if not value:
            raise RuntimeError(f"required secret '{name}' is not configured")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
