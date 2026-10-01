from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from ordering.domain.schemas.common import IdempotencyKey, Money, OutModel, StrictModel

OrderStatus = Literal["draft", "pending_approval", "approved", "submitted", "fulfilled", "cancelled", "payment_failed"]
PaymentStatus = Literal["not_started", "pending", "authorized", "captured", "failed", "refunded"]


class CreateOrderFromRecommendation(StrictModel):
    """Ordering MCP ``create_order`` input. Quantity may be adjusted by the user; price and
    vendor always come from the database (never from the LLM)."""

    recommendation_id: UUID
    quantity: int = Field(..., gt=0, le=100_000)
    approval_id: UUID
    idempotency_key: IdempotencyKey
    notes: str | None = Field(None, max_length=500)


class UpdateOrder(StrictModel):
    order_id: UUID
    quantity: int | None = Field(None, gt=0, le=100_000)
    expected_delivery_date: date | None = None
    expected_version: int = Field(..., ge=1, description="optimistic-lock version read by the caller")
    approval_id: UUID
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def _something_to_change(self):
        if self.quantity is None and self.expected_delivery_date is None:
            raise ValueError("nothing to update")
        return self


class CancelOrder(StrictModel):
    order_id: UUID
    reason: str = Field(..., min_length=3, max_length=300)
    approval_id: UUID
    idempotency_key: IdempotencyKey


class OrderFilter(StrictModel):
    status: OrderStatus | None = None
    payment_status: PaymentStatus | None = None
    sku: str | None = Field(None, pattern=r"^[A-Z0-9-]{3,32}$")
    location_code: str | None = Field(None, max_length=32)
    recommendation_id: UUID | None = None
    order_number: str | None = Field(None, pattern=r"^[A-Z]{1,4}-\d{4,12}$")
    limit: int = Field(10, ge=1, le=50)


class OrderLine(OutModel):
    line_no: int
    sku: str
    product_name: str
    ordered_qty: int
    unit_cost: Money
    line_total: Money


class Order(OutModel):
    order_id: UUID
    order_number: str
    recommendation_id: UUID | None
    location_code: str
    vendor_code: str
    vendor_name: str
    status: OrderStatus
    payment_status: PaymentStatus
    currency: str
    total_amount: Money
    expected_delivery_date: date
    created_by: str
    version: int
    created_at: datetime
    updated_at: datetime
    lines: list[OrderLine] = []
    replayed: bool = False
