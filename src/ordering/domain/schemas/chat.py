"""Public chatbot API contracts (API gateway ↔ clients, API gateway ↔ agent service)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from ordering.domain.schemas.common import StrictModel


class ChatRequest(StrictModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: UUID | None = None


class Citation(BaseModel):
    ref: int
    document: str
    version: str | None = None
    section: str | None = None
    chunk_id: str
    source_uri: str | None = None
    score: float | None = None


class PendingApproval(BaseModel):
    approval_id: UUID
    action: str
    summary: str
    required_role: str
    expires_at: datetime
    payload: dict[str, Any]


class ChatResponse(BaseModel):
    conversation_id: UUID
    message_id: UUID
    reply: str
    intent: str | None = None
    status: Literal["completed", "awaiting_approval", "awaiting_retry", "degraded", "refused", "error"] = "completed"
    pending_approval: PendingApproval | None = None
    citations: list[Citation] = []
    data: dict[str, Any] = {}
    trace_id: str | None = None
    agent_version: str | None = None
    disclosure: str = "AI-generated response. Order and payment facts come from system records."


class ConversationMessage(BaseModel):
    message_id: UUID
    role: str
    content: str
    intent: str | None
    created_at: datetime


class Conversation(BaseModel):
    conversation_id: UUID
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessage] = []
    pending_approval: PendingApproval | None = None


class FeedbackRequest(StrictModel):
    message_id: UUID | None = None
    rating: int = Field(..., ge=1, le=5)
    labels: list[Literal["wrong_recommendation", "hallucination", "unhelpful", "too_slow", "unsafe",
                         "great_answer", "wrong_order_details", "bad_citation"]] = []
    comment: str | None = Field(None, max_length=2000)
