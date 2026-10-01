from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from ordering.domain.schemas.common import IdempotencyKey, Money, OutModel, StrictModel

PaymentMethod = Literal["card", "ach", "invoice"]
PaymentState = Literal["pending", "authorized", "captured", "failed", "refunded"]


class ProcessPayment(StrictModel):
    """Payment MCP ``process_payment`` input.

    ``payment_method_token`` is a *simulated* token (see ``payment/simulator.py``):
    ``tok_visa`` / ``tok_mastercard`` succeed, ``tok_decline`` → card_declined,
    ``tok_insufficient`` → insufficient_funds, ``tok_processor_error`` → retryable processor error.
    The amount is always read from the order - callers cannot choose what to charge.
    """

    order_id: UUID
    method: PaymentMethod = "card"
    payment_method_token: str = Field("tok_visa", pattern=r"^tok_[a-z_]{2,32}$")
    approval_id: UUID
    idempotency_key: IdempotencyKey


class Payment(OutModel):
    payment_id: UUID
    order_id: UUID
    amount: Money
    currency: str
    method: PaymentMethod
    method_last4: str | None
    status: PaymentState
    provider_reference: str | None
    failure_code: str | None
    failure_reason: str | None
    attempt: int
    created_at: datetime
    replayed: bool = False


class PaymentStatusResult(OutModel):
    order_id: UUID
    order_payment_status: str
    latest: Payment | None
    attempts: int
