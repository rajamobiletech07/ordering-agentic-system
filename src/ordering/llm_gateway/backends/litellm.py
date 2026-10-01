"""LiteLLM backend: forwards to a LiteLLM proxy (``docker compose --profile litellm``).

The same route names are defined as LiteLLM ``model_name`` groups with ``fallbacks`` in
``docker/litellm/config.yaml``, so switching ``LLM_BACKEND=native|litellm`` needs no caller
change. Policy, auth, budgets and the usage ledger stay in this gateway.
"""

from __future__ import annotations

import time
import uuid

import httpx

from ordering.common.config import Settings
from ordering.common.errors import LLMUnavailable
from ordering.common.http import get_http_client
from ordering.llm_gateway.router import RoutingConfig, load_config
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


class LiteLLMBackend:
    name = "litellm"

    def __init__(self, settings: Settings, config: RoutingConfig | None = None):
        self.settings = settings
        self.config = config or load_config()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.secret('litellm_api_key')}"}

    async def chat(self, req: ChatCompletionRequest, tenant: str | None) -> ChatCompletionResponse:
        payload = {
            "model": req.model,
            "messages": [m.model_dump() for m in req.messages],
            "max_tokens": req.max_tokens or self.config.route(req.model).max_tokens,
            "metadata": {"tenant": tenant, "prompt_version": req.metadata.prompt_version},
        }
        if req.response_format and req.response_format.json_schema:
            spec = req.response_format.json_schema
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": spec.name, "schema": spec.schema_, "strict": True}}
        started = time.perf_counter()
        try:
            resp = await get_http_client().post(f"{self.settings.litellm_url}/v1/chat/completions", json=payload,
                                                headers=self._headers(), timeout=self.settings.llm_route_deadline_s)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            await record_attempt(tenant=tenant, route=req.model, provider="litellm", model=req.model, attempt=1,
                                 outcome="error", latency_ms=int((time.perf_counter() - started) * 1000))
            if req.metadata.fallback_text is not None:
                return self._build(req, req.metadata.fallback_text, "template", Usage(), 0.0, started, degraded=True)
            raise LLMUnavailable(f"litellm proxy failed: {exc.__class__.__name__}") from exc
        body = resp.json()
        usage = body.get("usage") or {}
        cost = float(resp.headers.get("x-litellm-response-cost", 0) or 0)
        model = body.get("model", req.model)
        u = Usage(prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
                  cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
                  total_tokens=usage.get("total_tokens", 0))
        await record_attempt(tenant=tenant, route=req.model, provider="litellm", model=model, attempt=1, outcome="ok",
                             input_tokens=u.prompt_tokens, output_tokens=u.completion_tokens,
                             cached_tokens=u.cached_tokens, cost=cost,
                             latency_ms=int((time.perf_counter() - started) * 1000),
                             prompt_version=req.metadata.prompt_version)
        return self._build(req, body["choices"][0]["message"]["content"] or "", model, u, cost, started)

    def _build(self, req, text, model, usage, cost, started, degraded=False) -> ChatCompletionResponse:
        latency = int((time.perf_counter() - started) * 1000)
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:24]}", model=model,
            choices=[Choice(message=ChatMessage(role="assistant", content=text))], usage=usage,
            x_gateway=GatewayInfo(route=req.model, route_config_version=self.config.version, provider="litellm",
                                  model=model, attempts=[Attempt(provider="litellm", model=model, attempt=1,
                                                                 outcome="degraded" if degraded else "ok",
                                                                 latency_ms=latency)],
                                  fallback_used=degraded, degraded=degraded, cost_usd=cost, backend=self.name),
        )

    async def embed(self, req: EmbeddingRequest, tenant: str | None) -> EmbeddingResponse:
        try:
            resp = await get_http_client().post(f"{self.settings.litellm_url}/v1/embeddings",
                                                json={"model": f"embed-{req.model}", "input": req.input},
                                                headers=self._headers(), timeout=30)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"litellm embeddings failed: {exc.__class__.__name__}") from exc
        body = resp.json()
        return EmbeddingResponse(model=body.get("model", req.model),
                                 data=[EmbeddingItem(index=d["index"], embedding=d["embedding"]) for d in body["data"]],
                                 usage=Usage(prompt_tokens=(body.get("usage") or {}).get("prompt_tokens", 0)))

    def health(self) -> dict:
        return {"litellm": {"configured": True, "url": self.settings.litellm_url}}
