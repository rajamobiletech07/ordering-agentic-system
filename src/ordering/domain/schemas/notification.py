from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from ordering.domain.schemas.common import IdempotencyKey, OutModel, StrictModel

Channel = Literal["email", "sms", "webhook", "in_app"]
NotificationStatus = Literal["queued", "sending", "delivered", "failed", "dead_letter"]
Template = Literal["order_confirmation", "payment_failed", "order_cancelled", "approval_requested",
                   "order_delivered", "payment_captured"]


class SendNotification(StrictModel):
    order_id: UUID | None = None
    channel: Channel = "email"
    template: Template
    # Recipient is optional: defaults to the requesting user's email from the directory.
    recipient: str | None = Field(None, max_length=200)
    data: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: IdempotencyKey


class Notification(OutModel):
    notification_id: UUID
    order_id: UUID | None
    channel: Channel
    recipient: str
    template: str
    status: NotificationStatus
    attempts: int
    max_attempts: int
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime
    replayed: bool = False
