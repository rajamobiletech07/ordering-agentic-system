from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from ordering.domain.schemas.common import OutModel, StrictModel

ApprovalAction = Literal["create_order", "process_payment", "update_order", "cancel_order"]
ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "consumed"]


class Approval(OutModel):
    approval_id: UUID
    conversation_id: UUID | None
    requested_by: str
    action: ApprovalAction
    payload: dict[str, Any]
    payload_hash: str
    summary: str
    required_role: str
    status: ApprovalStatus
    decided_by: str | None
    decided_at: datetime | None
    expires_at: datetime
    created_at: datetime


class ApprovalDecision(StrictModel):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(None, max_length=500)
