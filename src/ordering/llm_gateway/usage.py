"""Token usage & cost ledger (``llm_usage`` table + Prometheus) and per-tenant daily budgets."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from ordering.common.config import get_settings
from ordering.common.context import get_context
from ordering.common.db import tenant_session
from ordering.common.errors import BudgetExceeded
from ordering.common.logging import get_logger
from ordering.common.metrics import LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_TOKENS
from ordering.common.tracing import current_trace_id

log = get_logger("ordering.llm.usage")


def _budget_key(tenant: str) -> str:
    return f"llm:spend:{tenant}:{datetime.now(UTC):%Y%m%d}"


async def check_budget(tenant: str | None) -> None:
    if not tenant:
        return
    try:
        from ordering.common.redis import get_redis

        spent = float(await get_redis().get(_budget_key(tenant)) or 0)
    except Exception:  # budget store down → fail open, but log
        log.warning("budget_check_unavailable", tenant=tenant)
        return
    budget = get_settings().tenant_daily_budget_usd
    if spent >= budget:
        raise BudgetExceeded("tenant daily LLM budget exhausted", details={"spent_usd": round(spent, 4), "budget_usd": budget})


async def record_attempt(
    *,
    tenant: str | None,
    route: str,
    provider: str,
    model: str,
    attempt: int,
    outcome: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cost: float = 0.0,
    latency_ms: int = 0,
    prompt_version: str | None = None,
) -> None:
    LLM_CALLS.labels(route, provider, model, outcome).inc()
    if outcome == "ok":
        LLM_LATENCY.labels(provider, model).observe(latency_ms / 1000)
        LLM_TOKENS.labels(provider, model, "input").inc(input_tokens)
        LLM_TOKENS.labels(provider, model, "output").inc(output_tokens)
        LLM_TOKENS.labels(provider, model, "cached").inc(cached_tokens)
        LLM_COST.labels(tenant or "none", provider, model).inc(cost)
    ctx = get_context()
    try:
        async with tenant_session(tenant or "") as session:
            await session.execute(
                text(
                    """INSERT INTO llm_usage (tenant_id, route, provider, model, attempt, outcome, input_tokens,
                           output_tokens, cached_tokens, cost_usd, latency_ms, prompt_version, agent_version, trace_id,
                           request_id)
                       VALUES (:t, :r, :p, :m, :a, :o, :i, :out, :c, :cost, :lat, :pv, :av, :tid, :rid)"""
                ),
                {"t": tenant, "r": route, "p": provider, "m": model, "a": attempt, "o": outcome, "i": input_tokens,
                 "out": output_tokens, "c": cached_tokens, "cost": cost, "lat": latency_ms, "pv": prompt_version,
                 "av": get_settings().agent_version, "tid": current_trace_id(), "rid": ctx.request_id},
            )
        if cost and tenant:
            from ordering.common.redis import get_redis

            key = _budget_key(tenant)
            await get_redis().incrbyfloat(key, cost)
            await get_redis().expire(key, 60 * 60 * 36)
    except Exception as exc:  # never fail a user request because accounting failed
        log.warning("usage_record_failed", error=str(exc))
