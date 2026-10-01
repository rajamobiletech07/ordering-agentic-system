"""Explicit, typed handoff contracts between the supervisor and the business agents.

Agents never call each other directly: the supervisor sends a :class:`TaskRequest` and receives
a :class:`TaskResult`. Information sharing (e.g. a recommendation becoming an order proposal)
happens only through these validated structures, which are also persisted in the graph state
(durable, auditable, replayable).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ordering.domain.schemas.chat import Citation
from ordering.domain.schemas.common import Money

TaskType = Literal[
    "get_recommendations", "explain_recommendation", "investigate_risk", "policy_question", "build_proposal",
    "execute_order", "execute_payment", "execute_cancel", "order_status", "prepare_payment_retry", "prepare_cancel",
]


class TaskRequest(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    task_type: TaskType
    inputs: dict[str, Any] = Field(default_factory=dict)
    context_refs: dict[str, Any] = Field(default_factory=dict, description="IDs from conversation state")


class PaymentSelection(BaseModel):
    method: Literal["card", "ach", "invoice"] = "card"
    payment_method_token: str = Field("tok_visa", pattern=r"^tok_[a-z_]{2,32}$")
    label: str = "corporate card (simulated Visa ••4242)"


class OrderProposal(BaseModel):
    """Recommendation Agent → Supervisor → (human approval) → Ordering Agent."""

    recommendation_id: UUID
    sku: str
    product_name: str
    location_code: str
    vendor_code: str
    vendor_name: str
    quantity: int = Field(..., gt=0)
    recommended_qty: int
    unit_cost: Money
    total: Money
    currency: str = "USD"
    expected_delivery_date: date
    risk_level: str
    rationale: str
    requires_role: Literal["buyer", "approver"] = "buyer"
    payment: PaymentSelection = Field(default_factory=PaymentSelection)
    citations: list[Citation] = Field(default_factory=list)

    def approval_payload(self) -> dict[str, Any]:
        return {
            "recommendation_id": str(self.recommendation_id), "quantity": self.quantity, "sku": self.sku,
            "product_name": self.product_name, "location_code": self.location_code, "vendor_code": self.vendor_code,
            "unit_cost": f"{self.unit_cost:.2f}", "total": f"{self.total:.2f}", "currency": self.currency,
            "expected_delivery_date": self.expected_delivery_date.isoformat(),
            "payment": {"method": self.payment.method, "payment_method_token": self.payment.payment_method_token},
        }

    def summary(self) -> str:
        return (f"Order {self.quantity} × {self.sku} {self.product_name} for {self.location_code} from "
                f"{self.vendor_name} ({self.vendor_code}) at {self.currency} {self.unit_cost:.2f}/unit = "
                f"{self.currency} {self.total:,.2f}; expected delivery {self.expected_delivery_date:%a %d %b %Y}; "
                f"pay with {self.payment.label}.")


class PaymentOutcome(BaseModel):
    status: Literal["captured", "failed", "unavailable", "skipped"]
    payment_id: UUID | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    replayed: bool = False


class NotificationOutcome(BaseModel):
    status: Literal["queued", "delivered", "failed", "dead_letter", "unavailable", "skipped"]
    notification_id: UUID | None = None
    channel: str | None = None


class TaskResult(BaseModel):
    task_id: UUID
    status: Literal["succeeded", "partial", "failed", "needs_approval", "insufficient_evidence", "degraded"]
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    citations: list[Citation] = Field(default_factory=list)
    proposed_action: OrderProposal | dict[str, Any] | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))
