"""Deterministic mock provider (tests, CI, keyless demos).

It returns the caller-supplied ``fallback_text`` - the deterministic, template/rule-based answer
every agent computes alongside its LLM request. This keeps offline behaviour identical to the
product's graceful-degradation path, and makes E2E tests reproducible.

Special markers in the last user message let tests exercise gateway behaviour:
  ``[[mock:fail]]``      → transient failure (tests retry + fallback)
  ``[[mock:refuse]]``    → permanent failure (tests immediate fallback)
"""

from __future__ import annotations

import json

from ordering.llm_gateway.providers.base import (
    ChatProvider,
    PermanentProviderError,
    ProviderResult,
    TransientProviderError,
)


class MockProvider(ChatProvider):
    name = "mock"

    async def complete(self, *, model, system, messages, json_schema, max_tokens, timeout_s, options) -> ProviderResult:
        last = messages[-1]["content"] if messages else ""
        if "[[mock:fail]]" in last:
            raise TransientProviderError("mock transient failure")
        if "[[mock:refuse]]" in last:
            raise PermanentProviderError("mock refusal")
        text = options.get("fallback_text")
        if text is None:
            text = json.dumps({}) if json_schema is not None else "OK"
        return ProviderResult(
            text=text,
            input_tokens=sum(len(m["content"]) for m in messages) // 4 + len(system or "") // 4,
            output_tokens=len(text) // 4,
        )

    async def embed(self, *, model, texts, dims, timeout_s):
        from ordering.rag.embeddings.hashing import hashing_vector

        return [hashing_vector(t, dims) for t in texts], sum(len(t) // 4 for t in texts)
