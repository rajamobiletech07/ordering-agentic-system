"""Provider adapter interface.

Adapted from mcp-core ``python_code_gen_server/llm_engine/base.py`` (``EngineLM``) and
``factory.py``: same "one adapter per provider behind a common interface" idea, but async,
with structured-output support, token/cache usage reporting, and a normalized error taxonomy
so the router can decide between *retry*, *fallback* and *fail*. Retries live only in the
router (the SDK clients are created with ``max_retries=0``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    finish_reason: str = "stop"


class ProviderError(Exception):
    """Base provider error. ``transient`` errors may be retried on the same model."""

    transient = False

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class TransientProviderError(ProviderError):
    transient = True  # timeouts, 429, 5xx, connection resets


class PermanentProviderError(ProviderError):
    transient = False  # 400/401/403/404, refusals, unsupported params → next provider


class ProviderNotConfigured(PermanentProviderError):
    pass


class ChatProvider(ABC):
    name: str

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        timeout_s: float,
        options: dict[str, Any],
    ) -> ProviderResult: ...

    async def embed(self, *, model: str, texts: list[str], dims: int, timeout_s: float) -> tuple[list[list[float]], int]:
        raise PermanentProviderError(f"{self.name} does not support embeddings")
