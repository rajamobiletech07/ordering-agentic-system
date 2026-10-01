"""LLM Gateway routing: 3 providers per route, retry → fallback, validation, degradation."""

import json

import pytest

from ordering.common.config import Settings
from ordering.common.errors import LLMUnavailable
from ordering.llm_gateway.backends import native as native_mod
from ordering.llm_gateway.backends.native import NativeBackend
from ordering.llm_gateway.providers.base import (
    ChatProvider,
    PermanentProviderError,
    ProviderNotConfigured,
    ProviderResult,
    TransientProviderError,
)
from ordering.llm_gateway.router import cost_usd, load_config, parse_config
from ordering.llm_gateway.schemas import ChatCompletionRequest


class ScriptedProvider(ChatProvider):
    def __init__(self, name, script):
        self.name, self.script, self.calls = name, list(script), []

    async def complete(self, *, model, system, messages, json_schema, max_tokens, timeout_s, options):
        self.calls.append(model)
        step = self.script.pop(0) if self.script else "ok"
        if step == "transient":
            raise TransientProviderError(f"{self.name} 503")
        if step == "permanent":
            raise PermanentProviderError(f"{self.name} 400")
        if step == "missing":
            raise ProviderNotConfigured("no key")
        return ProviderResult(text=step if step != "ok" else f"answer from {self.name}", input_tokens=100,
                              output_tokens=20, cached_tokens=40)


@pytest.fixture(autouse=True)
def no_usage_writes(monkeypatch):
    recorded = []

    async def fake_record(**kw):
        recorded.append(kw)

    monkeypatch.setattr(native_mod, "record_attempt", fake_record)
    return recorded


def backend(anthropic, openai, gemini):
    s = Settings(llm_provider_override="")
    return NativeBackend(s, providers={"anthropic": anthropic, "openai": openai, "gemini": gemini,
                                       "mock": ScriptedProvider("mock", [])})


def req(route="planning", **kw):
    return ChatCompletionRequest(model=route, messages=[{"role": "user", "content": "hi"}], **kw)


def test_every_route_has_three_models_from_three_providers():
    cfg = load_config()
    assert cfg.routes, "no routes"
    for route in cfg.routes.values():
        assert [m.provider for m in route.models] == ["anthropic", "openai", "gemini"], route.name
    with pytest.raises(ValueError):
        parse_config({"routes": {"x": {"models": [{"provider": "anthropic", "model": "a"},
                                                  {"provider": "anthropic", "model": "b"},
                                                  {"provider": "openai", "model": "c"}]}}})


async def test_primary_success_no_fallback():
    a, o, g = ScriptedProvider("anthropic", ["ok"]), ScriptedProvider("openai", []), ScriptedProvider("gemini", [])
    resp = await backend(a, o, g).chat(req(), "acme-retail")
    assert resp.x_gateway.provider == "anthropic" and not resp.x_gateway.fallback_used
    assert resp.usage.cached_tokens == 40 and o.calls == [] and g.calls == []


async def test_transient_retry_then_fallback_to_third_provider(no_usage_writes):
    a = ScriptedProvider("anthropic", ["transient", "transient"])   # retried once, then give up
    o = ScriptedProvider("openai", ["permanent"])                   # 4xx → immediate fallback, no retry
    g = ScriptedProvider("gemini", ["ok"])
    resp = await backend(a, o, g).chat(req(), "acme-retail")
    assert resp.x_gateway.provider == "gemini" and resp.x_gateway.fallback_used
    assert [x.outcome for x in resp.x_gateway.attempts] == ["transient_error", "transient_error", "permanent_error", "ok"]
    assert len(a.calls) == 2 and len(o.calls) == 1
    assert [r["outcome"] for r in no_usage_writes] == ["transient_error", "transient_error", "permanent_error", "ok"]


async def test_unconfigured_provider_is_skipped():
    a, o, g = ScriptedProvider("anthropic", ["missing"]), ScriptedProvider("openai", ["ok"]), ScriptedProvider("gemini", [])
    resp = await backend(a, o, g).chat(req(), None)
    assert resp.x_gateway.provider == "openai"
    assert resp.x_gateway.attempts[0].outcome == "not_configured"


async def test_invalid_structured_output_falls_back():
    schema = {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"],
              "additionalProperties": False}
    a = ScriptedProvider("anthropic", ["not json"])
    o = ScriptedProvider("openai", [json.dumps({"wrong": 1})])
    g = ScriptedProvider("gemini", [json.dumps({"intent": "policy_question"})])
    resp = await backend(a, o, g).chat(req("intent_classification", response_format={
        "type": "json_schema", "json_schema": {"name": "intent", "schema": schema}}), None)
    assert resp.x_gateway.provider == "gemini"
    assert json.loads(resp.choices[0].message.content) == {"intent": "policy_question"}


async def test_all_providers_down_degrades_to_template_or_fails():
    def down():
        return (ScriptedProvider("anthropic", ["transient"] * 2), ScriptedProvider("openai", ["transient"] * 2),
                ScriptedProvider("gemini", ["permanent"]))
    resp = await backend(*down()).chat(req(metadata={"fallback_text": "deterministic answer"}), None)
    assert resp.x_gateway.degraded and resp.choices[0].message.content == "deterministic answer"
    with pytest.raises(LLMUnavailable):
        await backend(*down()).chat(req(), None)


async def test_tenant_provider_policy_filters_chain(monkeypatch):
    from ordering.governance import policy as policy_mod

    pol = policy_mod.get_policy().model_copy(deep=True)
    pol.tenants["restricted-tenant"] = {"allowed_providers": ["gemini"]}
    monkeypatch.setattr(native_mod, "get_policy", lambda: pol)
    a, o, g = ScriptedProvider("anthropic", ["ok"]), ScriptedProvider("openai", ["ok"]), ScriptedProvider("gemini", ["ok"])
    resp = await backend(a, o, g).chat(req(), "restricted-tenant")
    assert resp.x_gateway.provider == "gemini" and a.calls == [] and o.calls == []


async def test_direct_probe_calls_exact_model():
    a, o, g = ScriptedProvider("anthropic", []), ScriptedProvider("openai", ["ok"]), ScriptedProvider("gemini", [])
    resp = await backend(a, o, g).chat(req("openai/gpt-5.4-mini"), None)
    assert o.calls == ["gpt-5.4-mini"] and resp.x_gateway.route == "probe"


def test_cost_accounts_for_cached_tokens():
    cfg = load_config()
    # 1000 input (400 cached) + 100 output on claude-sonnet-5-5: 600*2 + 400*0.2 + 100*10 per 1M
    assert cost_usd(cfg, "claude-sonnet-5-5", 1000, 100, 400) == pytest.approx((600 * 2 + 400 * 0.2 + 100 * 10) / 1e6)
    assert cost_usd(cfg, "unknown-model", 1000, 100, 0) == 0.0
