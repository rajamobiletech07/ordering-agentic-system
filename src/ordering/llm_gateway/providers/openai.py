"""OpenAI adapter - adapted from mcp-core ``llm_engine/openai.py`` / ``llm_wrapper/utils.py``
(which used blocking ``requests`` inside async code). Async SDK client, strict JSON-schema
structured outputs, prompt-cache key per route, cached-token usage, embeddings."""

from __future__ import annotations

from typing import Any

import openai

from ordering.llm_gateway.providers.base import (
    ChatProvider,
    PermanentProviderError,
    ProviderNotConfigured,
    ProviderResult,
    TransientProviderError,
)

_TRANSIENT = (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError, openai.APITimeoutError)


class OpenAIProvider(ChatProvider):
    name = "openai"

    def __init__(self, api_key: str | None, base_url: str | None = None):
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0) if api_key else None

    def _require(self) -> openai.AsyncOpenAI:
        if self._client is None:
            raise ProviderNotConfigured("OPENAI_API_KEY not set")
        return self._client

    async def complete(self, *, model, system, messages, json_schema, max_tokens, timeout_s, options) -> ProviderResult:
        client = self._require()
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        msgs += [{"role": m["role"], "content": m["content"]} for m in messages]
        kwargs: dict[str, Any] = {"model": model, "messages": msgs, "max_completion_tokens": max_tokens}
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": options.get("schema_name", "result"), "schema": json_schema, "strict": True},
            }
        if options.get("route"):
            kwargs["prompt_cache_key"] = f"ordering-{options['route']}"
        try:
            response = await client.with_options(timeout=timeout_s).chat.completions.create(**kwargs)
        except _TRANSIENT as exc:
            raise TransientProviderError(f"openai transient: {exc.__class__.__name__}",
                                         status=getattr(exc, "status_code", None)) from exc
        except openai.APIStatusError as exc:
            raise PermanentProviderError(f"openai {exc.status_code}: {str(exc.message)[:200]}",
                                         status=exc.status_code) from exc
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise PermanentProviderError("openai refusal")
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None)
        return ProviderResult(
            text=choice.message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cached_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
            finish_reason=choice.finish_reason or "stop",
        )

    async def embed(self, *, model, texts, dims, timeout_s) -> tuple[list[list[float]], int]:
        client = self._require()
        try:
            response = await client.with_options(timeout=timeout_s).embeddings.create(
                model=model, input=[t.replace("\n", " ") for t in texts], dimensions=dims
            )
        except _TRANSIENT as exc:
            raise TransientProviderError(f"openai transient: {exc.__class__.__name__}") from exc
        except openai.APIStatusError as exc:
            raise PermanentProviderError(f"openai {exc.status_code}", status=exc.status_code) from exc
        return [d.embedding for d in response.data], response.usage.prompt_tokens
