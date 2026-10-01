"""Google Gemini adapter (google-genai SDK) - adapted from mcp-core ``llm_engine/gemini.py``."""

from __future__ import annotations

import asyncio
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ordering.llm_gateway.providers.base import (
    ChatProvider,
    PermanentProviderError,
    ProviderNotConfigured,
    ProviderResult,
    TransientProviderError,
)


def _classify(exc: genai_errors.APIError) -> Exception:
    code = getattr(exc, "code", None) or 0
    if code in (408, 429) or code >= 500:
        return TransientProviderError(f"gemini transient {code}", status=code)
    return PermanentProviderError(f"gemini {code}: {str(exc)[:200]}", status=code)


class GeminiProvider(ChatProvider):
    name = "gemini"

    def __init__(self, api_key: str | None):
        self._client = genai.Client(api_key=api_key) if api_key else None

    def _require(self) -> genai.Client:
        if self._client is None:
            raise ProviderNotConfigured("GOOGLE_API_KEY not set")
        return self._client

    async def complete(self, *, model, system, messages, json_schema, max_tokens, timeout_s, options) -> ProviderResult:
        client = self._require()
        contents = [
            types.Content(role="model" if m["role"] == "assistant" else "user", parts=[types.Part(text=m["content"])])
            for m in messages
        ]
        config: dict[str, Any] = {"max_output_tokens": max_tokens}
        if system:
            config["system_instruction"] = system
        if json_schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = json_schema
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model, contents=contents,  # type: ignore[arg-type]
                    config=types.GenerateContentConfig(**config)
                ),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            raise TransientProviderError("gemini timeout") from exc
        except genai_errors.APIError as exc:
            raise _classify(exc) from exc
        text = response.text or ""
        if not text:
            reason = response.candidates[0].finish_reason if response.candidates else "empty"
            raise PermanentProviderError(f"gemini empty response ({reason})")
        usage = response.usage_metadata
        return ProviderResult(
            text=text,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=(getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0),
            cached_tokens=getattr(usage, "cached_content_token_count", 0) or 0,
        )

    async def embed(self, *, model, texts, dims, timeout_s) -> tuple[list[list[float]], int]:
        client = self._require()
        try:
            response = await asyncio.wait_for(
                client.aio.models.embed_content(
                    model=model, contents=texts, config=types.EmbedContentConfig(output_dimensionality=dims)
                ),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            raise TransientProviderError("gemini timeout") from exc
        except genai_errors.APIError as exc:
            raise _classify(exc) from exc
        vectors = [list(e.values or []) for e in (response.embeddings or [])]
        return vectors, sum(len(t) // 4 for t in texts)
