"""Notification service worker: asynchronous delivery with retries and status tracking.

Deployed as an independent service (own scaling and failure domain). Queue = the
``notifications`` table (transactional outbox written by the Notification MCP server).

Loop:
  1. ``claim_due_notifications(n)`` - SECURITY DEFINER function doing
     ``UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED)``: safe with many workers.
  2. Deliver via the simulated channel.
  3. Success → ``delivered``; transient failure → ``failed`` with exponential backoff + jitter;
     permanent failure or attempts exhausted → ``dead_letter``.
  4. ``reclaim_stuck_notifications`` returns rows whose worker died mid-delivery (lease expiry).

Delivery is at-least-once; ``provider_message_id``/idempotency keys let downstream dedupe.
"""

from __future__ import annotations

import asyncio
import json
import random
import signal
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from ordering.common.config import get_settings
from ordering.common.db import dispose_engines, get_sessionmaker, tenant_session
from ordering.common.logging import configure_logging, get_logger
from ordering.common.metrics import NOTIFICATIONS
from ordering.common.tracing import setup_tracing, tracer
from ordering.notification_worker.channels import PermanentDeliveryError, TransientDeliveryError, deliver

log = get_logger("ordering.notification.worker")
BATCH = 20
LEASE_SECONDS = 60


def backoff_seconds(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    return min(cap, base ** attempt) * random.uniform(0.8, 1.2)


async def claim(batch: int = BATCH) -> list[dict]:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(text("SELECT reclaim_stuck_notifications(:s)"), {"s": LEASE_SECONDS})
        rows = await session.execute(text("SELECT * FROM claim_due_notifications(:n)"), {"n": batch})
        return [dict(r._mapping) for r in rows]


async def _mark(n: dict, status: str, error: str | None = None, next_at: datetime | None = None) -> None:
    async with tenant_session(n["tenant_id"]) as session:
        await session.execute(
            text(
                """UPDATE notifications SET status = :s, last_error = :e,
                          next_attempt_at = COALESCE(:next, next_attempt_at),
                          delivered_at = CASE WHEN :s = 'delivered' THEN now() ELSE delivered_at END
                   WHERE notification_id = :id"""
            ),
            {"s": status, "e": error, "next": next_at, "id": n["notification_id"]},
        )


async def process(n: dict) -> str:
    payload = n["payload"] if isinstance(n["payload"], dict) else json.loads(n["payload"])
    with tracer("ordering.notification").start_as_current_span("notification.deliver") as span:
        span.set_attribute("notification.channel", n["channel"])
        span.set_attribute("notification.attempt", n["attempts"])
        try:
            await deliver(n["channel"], n["recipient"], n["template"], payload, n["attempts"])
            await _mark(n, "delivered")
            outcome = "delivered"
        except PermanentDeliveryError as exc:
            await _mark(n, "dead_letter", str(exc))
            outcome = "dead_letter"
        except (TransientDeliveryError, TimeoutError, ConnectionError) as exc:
            if n["attempts"] >= n["max_attempts"]:
                await _mark(n, "dead_letter", f"attempts exhausted: {exc}")
                outcome = "dead_letter"
            else:
                next_at = datetime.now(UTC) + timedelta(seconds=backoff_seconds(n["attempts"]))
                await _mark(n, "failed", str(exc), next_at)
                outcome = "retry_scheduled"
    NOTIFICATIONS.labels(n["channel"], outcome).inc()
    log.info("notification_processed", notification_id=str(n["notification_id"]), outcome=outcome,
             attempt=n["attempts"], tenant_id=n["tenant_id"])
    return outcome


async def run_once(batch: int = BATCH) -> int:
    claimed = await claim(batch)
    if claimed:
        await asyncio.gather(*(process(n) for n in claimed))
    return len(claimed)


async def run_forever(stop: asyncio.Event) -> None:
    settings = get_settings()
    log.info("notification_worker_started", poll_s=settings.notification_worker_poll_s)
    while not stop.is_set():
        try:
            processed = await run_once()
        except Exception:
            log.exception("worker_iteration_failed")
            processed = 0
        if processed == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.notification_worker_poll_s)
            except TimeoutError:
                pass


async def main() -> None:
    settings = get_settings()
    settings.service_name = "notification-worker"
    configure_logging("notification-worker", settings.log_level)
    setup_tracing(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run_forever(stop)
    finally:
        await dispose_engines()
        log.info("notification_worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
