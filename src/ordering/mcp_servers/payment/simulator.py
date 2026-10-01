"""Deterministic payment processor simulator. NO real payments are ever made.

Outcomes are driven by the simulated token so demos and tests are reproducible:

=====================  ==========================================
token                  outcome
=====================  ==========================================
tok_visa, tok_mastercard, tok_amex, tok_ach, tok_invoice   captured
tok_decline            failed: card_declined
tok_insufficient       failed: insufficient_funds
tok_expired            failed: expired_card
tok_processor_error    retryable processor outage (raises)
anything else          failed: invalid_payment_method
=====================  ==========================================
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from decimal import Decimal

from ordering.common.errors import DependencyUnavailable

SUCCESS_TOKENS = {"tok_visa": "4242", "tok_mastercard": "4444", "tok_amex": "0005", "tok_ach": "6789", "tok_invoice": None}
DECLINES = {
    "tok_decline": ("card_declined", "The card was declined by the issuer (simulated)."),
    "tok_insufficient": ("insufficient_funds", "Insufficient funds (simulated)."),
    "tok_expired": ("expired_card", "The card has expired (simulated)."),
}


@dataclass(frozen=True)
class ChargeResult:
    status: str  # captured | failed
    last4: str | None
    provider_reference: str | None
    failure_code: str | None = None
    failure_reason: str | None = None


async def charge(amount: Decimal, currency: str, method: str, token: str, idempotency_key: str) -> ChargeResult:
    await asyncio.sleep(random.uniform(0.02, 0.12))  # simulated network latency
    if token == "tok_processor_error":  # noqa: S105  # nosec B105 - simulated token
        raise DependencyUnavailable("payment processor timeout (simulated)", details={"retryable": True})
    reference = "sim_" + hashlib.sha256(f"{idempotency_key}:{amount}:{currency}".encode()).hexdigest()[:20]
    if token in SUCCESS_TOKENS:
        return ChargeResult("captured", SUCCESS_TOKENS[token], reference)
    if token in DECLINES:
        code, reason = DECLINES[token]
        return ChargeResult("failed", "0002", None, code, reason)
    return ChargeResult("failed", None, None, "invalid_payment_method", "Unknown simulated payment token.")
