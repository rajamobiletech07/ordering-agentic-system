"""OpenAI-compatible request/response contracts (subset) plus gateway extensions.

Because the wire format is OpenAI-compatible, callers can point ``LLM_GATEWAY_URL`` at either
this gateway or a LiteLLM proxy without code changes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., max_length=200_000)


class JsonSchemaSpec(BaseModel):
    name: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    schema_: dict[str, Any] = Field(..., alias="schema")
    strict: bool = True

    model_config = ConfigDict(populate_by_name=True)


class ResponseFormat(BaseModel):
    type: Literal["text", "json_schema"] = "text"
    json_schema: JsonSchemaSpec | None = None


class GatewayMetadata(BaseModel):
    """Gateway extensions (ignored by plain OpenAI-compatible servers)."""

    prompt_id: str | None = None
    prompt_version: str | None = None
    conversation_id: str | None = None
    # Deterministic answer used by the mock provider and as last-resort degradation when every
    # provider fails. Lets the product keep working (clearly flagged) during a full LLM outage.
    fallback_text: str | None = Field(None, max_length=20_000)


class ChatCompletionRequest(BaseModel):
    model: str = Field(..., description="route name (e.g. 'planning') or 'provider/model' for a direct probe")
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=200)
    response_format: ResponseFormat | None = None
    max_tokens: int | None = Field(None, ge=1, le=64_000)
    temperature: float | None = Field(None, ge=0, le=2)
    metadata: GatewayMetadata = Field(default_factory=GatewayMetadata)


class Attempt(BaseModel):
    provider: str
    model: str
    attempt: int
    outcome: str
    latency_ms: int
    error: str | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0


class GatewayInfo(BaseModel):
    route: str
    route_config_version: str
    provider: str
    model: str
    attempts: list[Attempt]
    fallback_used: bool
    degraded: bool = False
    cost_usd: float = 0.0
    backend: str = "native"


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    model: str
    choices: list[Choice]
    usage: Usage
    x_gateway: GatewayInfo


class EmbeddingRequest(BaseModel):
    model: str = Field("openai", description="embedding profile name from routes.yaml (openai|gemini)")
    input: list[str] = Field(..., min_length=1, max_length=256)


class EmbeddingItem(BaseModel):
    index: int
    embedding: list[float]
    object: str = "embedding"


class EmbeddingResponse(BaseModel):
    object: str = "list"
    model: str
    data: list[EmbeddingItem]
    usage: Usage
