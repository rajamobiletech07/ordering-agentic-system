"""Anthropic (Claude) adapter - adapted from mcp-core ``llm_engine/anthropic.py``.

Changes vs the original: async client, no in-adapter tenacity retries (router owns retries),
structured outputs via ``output_config.format`` (JSON schema), per-route ``effort``, prompt
caching of the system prompt (``cache_control``), cache-aware usage reporting, ``refusal``
handling, and server-side refusal fallbacks on Claude 5.x models.
"""

from __future__ import annotations

from typing import Any

import anthropic

from ordering.llm_gateway.providers.base import (
    ChatProvider,
    PermanentProviderError,
    ProviderNotConfigured,
    ProviderResult,
    TransientProviderError,
)

# Claude 5.x: sampling params other than defaults are rejected; effort controls depth.
_EFFORT_MODELS = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")
# Models that accept the server-side refusal fallback ("default" routing by refusal category).
_SERVER_FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")

_TRANSIENT = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
)


class AnthropicProvider(ChatProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None):
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0) if api_key else None

    async def complete(self, *, model, system, messages, json_schema, max_tokens, timeout_s, options) -> ProviderResult:
        if self._client is None:
            raise ProviderNotConfigured("ANTHROPIC_API_KEY not set")
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }
        if system:
            # Stable system prompt first + cache breakpoint → prompt-cache hits across turns.
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        output_config: dict[str, Any] = {}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if options.get("effort") and model.startswith(_EFFORT_MODELS):
            output_config["effort"] = options["effort"]
        if output_config:
            kwargs["output_config"] = output_config
        client = self._client.with_options(timeout=timeout_s)
        try:
            if options.get("server_fallback", True) and model in _SERVER_FALLBACK_MODELS:
                response = await client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                response = await client.messages.create(**kwargs)
        except _TRANSIENT as exc:
            raise TransientProviderError(f"anthropic transient: {exc.__class__.__name__}",
                                         status=getattr(exc, "status_code", None)) from exc
        except anthropic.APIStatusError as exc:
            raise PermanentProviderError(f"anthropic {exc.status_code}: {exc.message[:200]}",
                                         status=exc.status_code) from exc

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise PermanentProviderError(f"anthropic refusal ({category})")
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        usage = response.usage
        cached = (getattr(usage, "cache_read_input_tokens", 0) or 0)
        return ProviderResult(
            text=text,
            input_tokens=(usage.input_tokens or 0) + cached + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
            output_tokens=usage.output_tokens or 0,
            cached_tokens=cached,
            finish_reason=response.stop_reason or "end_turn",
        )
