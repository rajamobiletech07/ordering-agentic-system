"""Human-in-the-loop approvals, cryptographically bound to the exact action payload.

The agent service records a proposal (``create``); the user decides via the API (``decide``);
the MCP server re-validates the approval inside the same DB transaction as the mutation
(``verify_and_consume``). Because the binding hash covers the action-defining fields, an LLM
(or an injected prompt) cannot change the quantity, recommendation, or payment method after
the human approved it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ordering.common.context import Principal
from ordering.common.errors import ApprovalRequired, NotFound, PermissionDenied
from ordering.common.ids import sha256_hex
from ordering.domain.schemas.approval import Approval

_COLS = """approval_id, conversation_id, requested_by, action, payload, payload_hash, summary, required_role, status,
           decided_by, decided_at, expires_at, created_at"""

# Fields that define each action. Only these are hashed/verified.
BINDING_FIELDS: dict[str, tuple[str, ...]] = {
    "create_order": ("recommendation_id", "quantity"),
    "process_payment": ("order_id", "method", "payment_method_token"),
    "update_order": ("order_id", "quantity", "expected_delivery_date"),
    "cancel_order": ("order_id",),
}


def binding_hash(action: str, fields: dict[str, Any]) -> str:
    keys = BINDING_FIELDS[action]
    return sha256_hex({"action": action, **{k: (str(fields.get(k)) if fields.get(k) is not None else None) for k in keys}})


async def create(
    session: AsyncSession,
    *,
    principal: Principal,
    conversation_id: UUID | None,
    action: str,
    payload: dict[str, Any],
    summary: str,
    required_role: str,
    ttl_minutes: int,
) -> Approval:
    approval_id = uuid4()
    row = (
        await session.execute(
            text(
                f"""INSERT INTO approvals (approval_id, tenant_id, conversation_id, requested_by, action, payload,
                        payload_hash, summary, required_role, expires_at)
                    VALUES (:id, :t, :c, :u, :a, CAST(:p AS JSONB), :h, :s, :r, :e) RETURNING {_COLS}"""
            ),
            {
                "id": approval_id,
                "t": principal.tenant_id,
                "c": conversation_id,
                "u": principal.user_id,
                "a": action,
                "p": json.dumps(payload, default=str),
                "h": binding_hash(action, payload),
                "s": summary,
                "r": required_role,
                "e": datetime.now(UTC) + timedelta(minutes=ttl_minutes),
            },
        )
    ).one()
    return Approval.model_validate(dict(row._mapping))


async def get(session: AsyncSession, approval_id: UUID, for_update: bool = False) -> Approval:
    row = (
        await session.execute(
            text(f"SELECT {_COLS} FROM approvals WHERE approval_id = :id" + (" FOR UPDATE" if for_update else "")),
            {"id": approval_id},
        )
    ).first()
    if row is None:
        raise NotFound("approval not found", details={"approval_id": str(approval_id)})
    return Approval.model_validate(dict(row._mapping))


async def pending_for_conversation(session: AsyncSession, conversation_id: UUID) -> Approval | None:
    row = (
        await session.execute(
            text(
                f"""SELECT {_COLS} FROM approvals WHERE conversation_id = :c AND status = 'pending' AND expires_at > now()
                    ORDER BY created_at DESC LIMIT 1"""
            ),
            {"c": conversation_id},
        )
    ).first()
    return Approval.model_validate(dict(row._mapping)) if row else None


async def expire_pending(session: AsyncSession, conversation_id: UUID) -> None:
    await session.execute(
        text("UPDATE approvals SET status = 'expired' WHERE conversation_id = :c AND status = 'pending'"),
        {"c": conversation_id},
    )


async def decide(session: AsyncSession, approval_id: UUID, principal: Principal, approve: bool,
                 comment: str | None) -> Approval:
    approval = await get(session, approval_id, for_update=True)
    if approval.status != "pending":
        raise ApprovalRequired(f"approval is {approval.status}", details={"status": approval.status})
    if approval.expires_at <= datetime.now(UTC):
        await session.execute(text("UPDATE approvals SET status = 'expired' WHERE approval_id = :id"), {"id": approval_id})
        raise ApprovalRequired("approval expired")
    if approve:
        if not principal.has_role(approval.required_role, "admin"):
            raise PermissionDenied(
                f"approving this action requires role '{approval.required_role}'",
                details={"required_role": approval.required_role},
            )
        # Four-eyes principle for high-value actions.
        if approval.required_role == "approver" and principal.user_id == approval.requested_by:
            raise PermissionDenied("high-value actions must be approved by a different user")
    elif principal.user_id != approval.requested_by and not principal.has_role("approver", "admin"):
        raise PermissionDenied("only the requester or an approver can reject")
    row = (
        await session.execute(
            text(
                f"""UPDATE approvals SET status = :s, decided_by = :u, decided_at = now(), decision_comment = :c
                    WHERE approval_id = :id RETURNING {_COLS}"""
            ),
            {"s": "approved" if approve else "rejected", "u": principal.user_id, "c": comment, "id": approval_id},
        )
    ).one()
    return Approval.model_validate(dict(row._mapping))


async def verify_and_consume(
    session: AsyncSession, approval_id: UUID, action: str, fields: dict[str, Any], consume: bool = True
) -> Approval:
    """Validate the approval for this exact action inside the caller's transaction."""
    approval = await get(session, approval_id, for_update=True)
    allowed = {"approved"} if consume else {"approved", "consumed"}
    if approval.status not in allowed:
        raise ApprovalRequired(f"approval {approval_id} is {approval.status}", details={"status": approval.status})
    if approval.expires_at <= datetime.now(UTC) and approval.status == "approved":
        raise ApprovalRequired("approval expired")
    if approval.action != action:
        raise ApprovalRequired("approval does not cover this action", details={"approved_action": approval.action})
    if approval.payload_hash != binding_hash(action, fields):
        raise ApprovalRequired(
            "requested parameters differ from what the user approved",
            details={"approved": {k: approval.payload.get(k) for k in BINDING_FIELDS[action]}},
        )
    if consume:
        await session.execute(text("UPDATE approvals SET status = 'consumed' WHERE approval_id = :id"), {"id": approval_id})
    return approval
