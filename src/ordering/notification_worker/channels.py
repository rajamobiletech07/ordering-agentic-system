"""Simulated delivery channels. Nothing leaves the system: deliveries are logged only.

Deterministic failure rules for demos/tests:
  * recipient containing ``bounce`` → permanent failure (goes straight to dead_letter)
  * recipient containing ``flaky``  → fails the first two attempts, then succeeds
  * fault flag ``notification:flaky|unavailable`` → injected transient failures
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from ordering.common.faults import active_fault
from ordering.common.logging import get_logger
from ordering.common.redaction import redact_text

log = get_logger("ordering.notification.channels")


class TransientDeliveryError(Exception):
    pass


class PermanentDeliveryError(Exception):
    pass


@dataclass(frozen=True)
class DeliveryReceipt:
    provider_message_id: str


async def deliver(channel: str, recipient: str, template: str, payload: dict, attempt: int) -> DeliveryReceipt:
    await asyncio.sleep(random.uniform(0.01, 0.05))
    fault = await active_fault("notification")
    if fault == "unavailable" or (fault == "flaky" and random.random() < 0.5):
        raise TransientDeliveryError(f"{channel} provider unavailable (injected)")
    if "bounce" in recipient:
        raise PermanentDeliveryError("recipient rejected (simulated hard bounce)")
    if "flaky" in recipient and attempt <= 2:
        raise TransientDeliveryError("temporary provider error (simulated)")
    if channel == "sms" and not recipient.startswith("+"):
        raise PermanentDeliveryError("invalid phone number")
    log.info("notification_delivered_simulated", channel=channel, recipient=redact_text(recipient), template=template,
             subject=payload.get("subject"))
    return DeliveryReceipt(provider_message_id=f"sim-{channel}-{payload.get('id', '')[:8]}-{attempt}")
