"""Public chatbot API (v1). Every route: JWT authentication, permission check, per-user rate
limit, pydantic validation, then forwarding to the agent service."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from ordering.api_gateway.clients.agent_service import forward
from ordering.api_gateway.middleware.security import rate_limited
from ordering.common.context import Principal
from ordering.common.errors import PermissionDenied
from ordering.domain.schemas.approval import ApprovalDecision
from ordering.domain.schemas.chat import ChatRequest, ChatResponse, Conversation, FeedbackRequest
from ordering.security.permissions import (
    AUDIT_READ,
    CACHE_ADMIN,
    CHAT,
    FEEDBACK_WRITE,
    MEMORY_MANAGE,
    ORD_READ,
    has_permissions,
)

router = APIRouter(prefix="/v1", tags=["chatbot"])


def _require(principal: Principal, *perms: str) -> None:
    if not has_permissions(principal.roles, *perms):
        raise PermissionDenied("insufficient permissions", details={"required": list(perms)})


@router.post("/chat", response_model=ChatResponse, summary="Send a chat message (optionally continuing a conversation)")
async def chat(body: ChatRequest, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CHAT)
    return await forward(request, "POST", "/internal/chat", json=body.model_dump(mode="json"))


@router.get("/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: UUID, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CHAT)
    return await forward(request, "GET", f"/internal/conversations/{conversation_id}")


@router.post("/conversations/{conversation_id}/resume", response_model=ChatResponse,
             summary="Recover a turn that failed mid-workflow from its last checkpoint")
async def resume(conversation_id: UUID, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CHAT)
    return await forward(request, "POST", f"/internal/conversations/{conversation_id}/resume")


@router.post("/conversations/{conversation_id}/close", summary="Close a conversation (clears its cache)")
async def close(conversation_id: UUID, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CHAT)
    return await forward(request, "POST", f"/internal/conversations/{conversation_id}/close")


@router.get("/conversations/{conversation_id}/cache", summary="Thread cache statistics")
async def cache_stats(conversation_id: UUID, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CHAT)
    return await forward(request, "GET", f"/internal/conversations/{conversation_id}/cache")


@router.delete("/conversations/{conversation_id}/cache", summary="Clear the thread cache")
async def clear_cache(conversation_id: UUID, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CHAT)
    return await forward(request, "DELETE", f"/internal/conversations/{conversation_id}/cache")


@router.post("/conversations/{conversation_id}/feedback", status_code=201, summary="Rate an assistant reply")
async def feedback(conversation_id: UUID, body: FeedbackRequest, request: Request,
                   principal: Principal = Depends(rate_limited)):
    _require(principal, FEEDBACK_WRITE)
    return await forward(request, "POST", f"/internal/conversations/{conversation_id}/feedback",
                         json=body.model_dump(mode="json"))


@router.post("/approvals/{approval_id}", response_model=ChatResponse,
             summary="Approve or reject a pending action (resumes the workflow)")
async def decide(approval_id: UUID, body: ApprovalDecision, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CHAT)
    return await forward(request, "POST", f"/internal/approvals/{approval_id}", json=body.model_dump(mode="json"))


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: UUID, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CHAT)
    return await forward(request, "GET", f"/internal/approvals/{approval_id}")


@router.get("/memory", summary="List my long-term memories")
async def list_memory(request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, MEMORY_MANAGE)
    return await forward(request, "GET", "/internal/memory")


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: UUID, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, MEMORY_MANAGE)
    return await forward(request, "DELETE", f"/internal/memory/{memory_id}")


@router.delete("/memory", summary="Erase all my memories and thread caches (right to erasure)")
async def erase_memory(request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, MEMORY_MANAGE)
    return await forward(request, "DELETE", "/internal/memory")


@router.get("/orders/{order_id}", summary="Authoritative order, payment and notification status")
async def get_order(order_id: UUID, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, ORD_READ)
    return await forward(request, "GET", f"/internal/orders/{order_id}")


@router.get("/audit", summary="Audit trail (admin)")
async def audit(request: Request, limit: int = Query(50, ge=1, le=500), action: str | None = None,
                principal: Principal = Depends(rate_limited)):
    _require(principal, AUDIT_READ)
    return await forward(request, "GET", "/internal/audit", params={"limit": limit, **({"action": action} if action else {})})


@router.post("/dev/faults", summary="Inject a dependency fault (admin, non-production only)")
async def dev_faults(body: dict, request: Request, principal: Principal = Depends(rate_limited)):
    _require(principal, CACHE_ADMIN)
    return await forward(request, "POST", "/internal/dev/faults", json=body)
