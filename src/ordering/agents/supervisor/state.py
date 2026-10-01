"""Supervisor graph state (checkpointed to Postgres after every node - durable workflow state)."""

from __future__ import annotations

from typing import Any, TypedDict


class SupervisorState(TypedDict, total=False):
    # identity snapshot (verified at the API boundary; used to act on the requester's behalf)
    conversation_id: str
    tenant_id: str
    user_id: str
    roles: list[str]
    # current turn
    user_message: str
    message_id: str
    request_id: str
    blocked: bool
    intent: dict[str, Any]
    response: dict[str, Any]
    # short-term memory
    messages: list[dict[str, str]]
    summary: str
    memories: list[dict[str, Any]]
    # shared working context (information exchanged between agents via the supervisor)
    last_recommendations: list[dict[str, Any]]
    focus_recommendation_id: str | None
    proposal: dict[str, Any] | None
    # human-in-the-loop workflow
    pending_approval: dict[str, Any] | None
    approval_decision: dict[str, Any] | None
    order_approval_id: str | None
    order: dict[str, Any] | None
    workflow: str  # idle | awaiting_approval | executing | awaiting_retry | completed
    task_log: list[dict[str, Any]]
    turn: int
