"""Agent-side client for the LLM Gateway.

* Every call names a *route* (request type); the gateway picks models and handles fallback.
* Every call carries a deterministic ``fallback`` answer: used by the mock provider, and as the
  degraded response if the gateway or every provider is down - the assistant keeps working
  (clearly flagged) instead of failing.
* Structured outputs are validated against pydantic models here as well (defence in depth).
* Deterministic routes are cached per chat thread (see ``agents/cache.py``).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ordering.common.config import get_settings
from ordering.common.context import Principal, get_context
from ordering.common.errors import OrderingError, error_from_dict
from ordering.common.http import get_http_client, propagation_headers
from ordering.common.ids import sha256_hex
from ordering.common.logging import get_logger
from ordering.security.service_tokens import mint_service_token

log = get_logger("ordering.llm.client")
T = TypeVar("T", bound=BaseModel)

_SYSTEM_PRINCIPAL = Principal(user_id="system", tenant_id="system", roles=frozenset({"viewer"}))


@dataclass
class LLMResult:
    text: str
    parsed: Any = None
    provider: str = "unknown"
    model: str = "unknown"
    degraded: bool = False
    fallback_used: bool = False
    cache_hit: bool = False
    cost_usd: float = 0.0
    attempts: int = 0


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema → strict structured-output schema accepted by Claude/OpenAI/Gemini:
    ``$ref`` inlined, every object closed (``additionalProperties: false``) with all properties
    required (optional fields become nullable), and unsupported keywords removed."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            node = {k: resolve(v) for k, v in node.items() if k not in ("title", "default", "examples", "format",
                                                                          "minLength", "maxLength", "pattern",
                                                                          "minimum", "maximum", "exclusiveMinimum",
                                                                          "exclusiveMaximum", "minItems", "maxItems")}
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            return node
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


class LLMClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or get_settings().llm_gateway_url

    def _headers(self) -> dict[str, str]:
        ctx = get_context()
        principal = ctx.principal or _SYSTEM_PRINCIPAL
        token = mint_service_token(principal, ctx.agent or "supervisor", ctx.conversation_id)
        return propagation_headers({"Authorization": f"Bearer {token}"})

    async def complete(
        self,
        route: str,
        *,
        system: str,
        user: str,
        history: list[dict[str, str]] | None = None,
        schema: type[T] | None = None,
        fallback: str | BaseModel | dict | None = None,
        prompt_id: str | None = None,
        prompt_version: str | None = None,
        max_tokens: int | None = None,
        cache: Any | None = None,
    ) -> LLMResult:
        fallback_text = None
        if fallback is not None:
            fallback_text = (fallback.model_dump_json() if isinstance(fallback, BaseModel)
                             else json.dumps(fallback) if isinstance(fallback, dict) else str(fallback))
        messages = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]
        body: dict[str, Any] = {
            "model": route,
            "messages": messages,
            "metadata": {"prompt_id": prompt_id, "prompt_version": prompt_version,
                         "conversation_id": get_context().conversation_id, "fallback_text": fallback_text},
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": schema.__name__, "schema": strict_json_schema(schema)}}

        cache_key = None
        if cache is not None:
            cache_key = sha256_hex({"route": route, "pv": prompt_version, "m": messages,
                                    "s": schema.__name__ if schema else None})
            cached = await cache.get("llm", cache_key)
            if cached is not None:
                result = LLMResult(**cached)
                result.cache_hit = True
                if schema is not None:
                    result.parsed = schema.model_validate_json(result.text)
                return result

        try:
            resp = await get_http_client().post(f"{self.base_url}/v1/chat/completions", json=body,
                                                headers=self._headers(), timeout=get_settings().llm_route_deadline_s + 5)
            if resp.status_code >= 400:
                try:
                    raise error_from_dict(resp.json())
                except (ValueError, KeyError):
                    raise OrderingError(f"llm gateway error {resp.status_code}") from None
            data = resp.json()
            info = data.get("x_gateway", {})
            result = LLMResult(
                text=data["choices"][0]["message"]["content"],
                provider=info.get("provider", "unknown"),
                model=data.get("model", "unknown"),
                degraded=bool(info.get("degraded")),
                fallback_used=bool(info.get("fallback_used")),
                cost_usd=float(info.get("cost_usd", 0.0)),
                attempts=len(info.get("attempts", [])),
            )
        except (httpx.HTTPError, OrderingError) as exc:
            if fallback_text is None:
                raise
            log.warning("llm_gateway_unreachable_degrading", route=route, error=str(exc)[:200])
            result = LLMResult(text=fallback_text, provider="template", model="template", degraded=True,
                               fallback_used=True)

        if schema is not None:
            try:
                result.parsed = schema.model_validate_json(result.text)
            except ValidationError:
                if fallback_text is None:
                    raise
                log.warning("llm_structured_output_invalid", route=route, provider=result.provider)
                result.text, result.degraded = fallback_text, True
                result.parsed = schema.model_validate_json(fallback_text)

        if cache is not None and cache_key and not result.degraded:
            await cache.set("llm", cache_key, {k: v for k, v in result.__dict__.items() if k != "parsed"})
        return result

    async def embed(self, texts: list[str], profile: str = "openai") -> list[list[float]]:
        resp = await get_http_client().post(f"{self.base_url}/v1/embeddings", json={"model": profile, "input": texts},
                                            headers=self._headers(), timeout=60)
        if resp.status_code >= 400:
            raise error_from_dict(resp.json())
        return [item["embedding"] for item in sorted(resp.json()["data"], key=lambda d: d["index"])]


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def set_llm_client(client: LLMClient | None) -> None:
    global _client
    _client = client
