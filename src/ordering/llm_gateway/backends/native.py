"""Native backend: in-house provider adapters with retry → fallback across 3 providers."""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid

import jsonschema

from ordering.common.config import Settings
from ordering.common.errors import LLMUnavailable, ValidationFailed
from ordering.common.logging import get_logger
from ordering.common.metrics import LLM_FALLBACKS
from ordering.decorators.resilience import CircuitBreaker
from ordering.governance.policy import get_policy
from ordering.llm_gateway.providers.anthropic import AnthropicProvider
from ordering.llm_gateway.providers.base import (
    ChatProvider,
    PermanentProviderError,
    ProviderNotConfigured,
    TransientProviderError,
)
from ordering.llm_gateway.providers.gemini import GeminiProvider
from ordering.llm_gateway.providers.mock import MockProvider
from ordering.llm_gateway.providers.openai import OpenAIProvider
from ordering.llm_gateway.router import ModelTarget, Route, RoutingConfig, cost_usd, load_config
from ordering.llm_gateway.schemas import (
    Attempt,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    EmbeddingItem,
    EmbeddingRequest,
    EmbeddingResponse,
    GatewayInfo,
    Usage,
)
from ordering.llm_gateway.usage import record_attempt

log = get_logger("ordering.llm.native")


class NativeBackend:
    name = "native"

    def __init__(self, settings: Settings, config: RoutingConfig | None = None,
                 providers: dict[str, ChatProvider] | None = None):
        self.settings = settings
        self.config = config or load_config()
        self.providers: dict[str, ChatProvider] = providers or {
            "anthropic": AnthropicProvider(settings.secret("anthropic_api_key")),
            "openai": OpenAIProvider(settings.secret("openai_api_key")),
            "gemini": GeminiProvider(settings.secret("google_api_key")),
            "mock": MockProvider(),
        }
        self.breakers = {name: CircuitBreaker(f"llm:{name}", failure_threshold=5, reset_timeout_s=30)
                         for name in self.providers}

    # ------------------------------------------------------------------ chat
    def _chain(self, route: Route, tenant: str | None) -> list[ModelTarget]:
        if self.settings.llm_provider_override == "mock":
            return [ModelTarget("mock", "mock")]
        allowed = get_policy().allowed_providers(tenant)
        return [m for m in route.models if m.provider in allowed]

    async def chat(self, req: ChatCompletionRequest, tenant: str | None) -> ChatCompletionResponse:
        if "/" in req.model:  # direct probe: provider/model, no fallback
            provider_name, model_name = req.model.split("/", 1)
            if provider_name not in self.providers:
                raise ValidationFailed(f"unknown provider {provider_name}")
            route = Route(name="probe", models=(ModelTarget(provider_name, model_name),), timeout_s=30,
                          max_tokens=req.max_tokens or 256, retries_per_model=0)
            chain = [ModelTarget(provider_name, model_name)]
        else:
            route = self.config.route(req.model)
            chain = self._chain(route, tenant)

        system = "\n\n".join(m.content for m in req.messages if m.role == "system") or None
        messages = [{"role": m.role, "content": m.content} for m in req.messages if m.role != "system"]
        spec = req.response_format.json_schema if req.response_format else None
        schema = spec.schema_ if spec else None
        schema_name = spec.name if spec else None
        max_tokens = req.max_tokens or route.max_tokens
        deadline = time.monotonic() + self.settings.llm_route_deadline_s
        attempts: list[Attempt] = []

        for index, target in enumerate(chain):
            provider = self.providers[target.provider]
            breaker = self.breakers[target.provider]
            if breaker.state == "open":
                attempts.append(Attempt(provider=target.provider, model=target.model, attempt=0,
                                        outcome="circuit_open", latency_ms=0))
                continue
            for attempt in range(1, route.retries_per_model + 2):
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    break
                started = time.perf_counter()
                options = {**target.options, "route": route.name, "schema_name": schema_name,
                           "fallback_text": req.metadata.fallback_text}
                try:
                    result = await provider.complete(
                        model=target.model, system=system, messages=messages, json_schema=schema,
                        max_tokens=max_tokens, timeout_s=min(route.timeout_s, remaining), options=options,
                    )
                    if schema is not None:
                        try:
                            jsonschema.validate(json.loads(result.text), schema)
                        except (ValueError, jsonschema.ValidationError) as exc:
                            raise PermanentProviderError(f"invalid structured output: {str(exc)[:120]}") from exc
                except ProviderNotConfigured as exc:
                    attempts.append(Attempt(provider=target.provider, model=target.model, attempt=attempt,
                                            outcome="not_configured", latency_ms=0, error=str(exc)))
                    break
                except (TransientProviderError, PermanentProviderError) as exc:
                    latency = int((time.perf_counter() - started) * 1000)
                    outcome = "transient_error" if exc.transient else "permanent_error"
                    attempts.append(Attempt(provider=target.provider, model=target.model, attempt=attempt,
                                            outcome=outcome, latency_ms=latency, error=str(exc)[:200]))
                    await record_attempt(tenant=tenant, route=route.name, provider=target.provider, model=target.model,
                                         attempt=attempt, outcome=outcome, latency_ms=latency,
                                         prompt_version=req.metadata.prompt_version)
                    if exc.transient:
                        await breaker.on_failure()
                        if attempt <= route.retries_per_model:
                            await asyncio.sleep(random.uniform(0.2, 0.8) * attempt)
                            continue
                    break
                else:
                    await breaker.on_success()
                    latency = int((time.perf_counter() - started) * 1000)
                    cost = cost_usd(self.config, target.model, result.input_tokens, result.output_tokens,
                                    result.cached_tokens)
                    attempts.append(Attempt(provider=target.provider, model=target.model, attempt=attempt,
                                            outcome="ok", latency_ms=latency))
                    await record_attempt(tenant=tenant, route=route.name, provider=target.provider, model=target.model,
                                         attempt=attempt, outcome="ok", input_tokens=result.input_tokens,
                                         output_tokens=result.output_tokens, cached_tokens=result.cached_tokens,
                                         cost=cost, latency_ms=latency, prompt_version=req.metadata.prompt_version)
                    fallback_used = index > 0 or any(a.outcome != "ok" for a in attempts)
                    if index > 0:
                        LLM_FALLBACKS.labels(route.name, chain[0].provider).inc()
                    return self._response(route, target, result.text, attempts, fallback_used, cost,
                                          Usage(prompt_tokens=result.input_tokens, completion_tokens=result.output_tokens,
                                                cached_tokens=result.cached_tokens,
                                                total_tokens=result.input_tokens + result.output_tokens),
                                          result.finish_reason)
        # every provider failed
        if req.metadata.fallback_text is not None:
            log.warning("llm_degraded_template", route=route.name, attempts=len(attempts))
            await record_attempt(tenant=tenant, route=route.name, provider="template", model="template",
                                 attempt=0, outcome="degraded", prompt_version=req.metadata.prompt_version)
            return self._response(route, ModelTarget("template", "template"), req.metadata.fallback_text, attempts,
                                  True, 0.0, Usage(), "degraded", degraded=True)
        raise LLMUnavailable("all LLM providers failed", details={"attempts": [a.model_dump() for a in attempts]})

    def _response(self, route, target, text, attempts, fallback_used, cost, usage, finish, degraded=False):
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
            model=target.model,
            choices=[Choice(message=ChatMessage(role="assistant", content=text), finish_reason=finish)],
            usage=usage,
            x_gateway=GatewayInfo(route=route.name, route_config_version=self.config.version, provider=target.provider,
                                  model=target.model, attempts=attempts, fallback_used=fallback_used,
                                  degraded=degraded, cost_usd=cost, backend=self.name),
        )

    # ------------------------------------------------------------------ embeddings
    async def embed(self, req: EmbeddingRequest, tenant: str | None) -> EmbeddingResponse:
        target = self.config.embeddings.get(req.model)
        if target is None:
            raise ValidationFailed(f"unknown embedding profile '{req.model}'", details={"profiles": list(self.config.embeddings)})
        provider_name = "mock" if self.settings.llm_provider_override == "mock" else target.provider
        started = time.perf_counter()
        try:
            vectors, tokens = await self.providers[provider_name].embed(
                model=target.model, texts=req.input, dims=target.dims, timeout_s=30
            )
        except (TransientProviderError, PermanentProviderError) as exc:
            await record_attempt(tenant=tenant, route=f"embed:{req.model}", provider=provider_name, model=target.model,
                                 attempt=1, outcome="error", latency_ms=int((time.perf_counter() - started) * 1000))
            # No cross-provider fallback: vector spaces are incompatible. Callers degrade to keyword search.
            raise LLMUnavailable(f"embedding provider failed: {exc}") from exc
        cost = cost_usd(self.config, target.model, tokens, 0, 0)
        await record_attempt(tenant=tenant, route=f"embed:{req.model}", provider=provider_name, model=target.model,
                             attempt=1, outcome="ok", input_tokens=tokens, cost=cost,
                             latency_ms=int((time.perf_counter() - started) * 1000))
        return EmbeddingResponse(
            model=target.model,
            data=[EmbeddingItem(index=i, embedding=v) for i, v in enumerate(vectors)],
            usage=Usage(prompt_tokens=tokens, total_tokens=tokens),
        )

    def health(self) -> dict:
        configured = {
            "anthropic": bool(self.settings.secret("anthropic_api_key")),
            "openai": bool(self.settings.secret("openai_api_key")),
            "gemini": bool(self.settings.secret("google_api_key")),
            "mock": True,
        }
        return {name: {"configured": configured.get(name, False), "circuit": self.breakers[name].state}
                for name in self.providers}
