"""Storage for the ``@idempotent`` decorator (see ``decorators/idempotency.py``)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from ordering.common.db import tenant_session
from ordering.common.errors import Conflict, IdempotencyConflict

# An in-progress claim older than this is considered abandoned (crashed worker) and may be retaken.
LEASE_SECONDS = 60


async def begin(tenant_id: str, scope: str, key: str, request_hash: str) -> Any | None:
    """Claim the key. Returns the stored response for a completed replay, else ``None``."""
    async with tenant_session(tenant_id) as session:
        inserted = await session.execute(
            text(
                """INSERT INTO idempotency_keys (tenant_id, scope, key, request_hash, status)
                   VALUES (:t, :s, :k, :h, 'in_progress') ON CONFLICT DO NOTHING RETURNING key"""
            ),
            {"t": tenant_id, "s": scope, "k": key, "h": request_hash},
        )
        if inserted.first() is not None:
            return None
        row = (
            await session.execute(
                text(
                    """SELECT request_hash, status, response,
                              updated_at < now() - make_interval(secs => :lease) AS stale
                       FROM idempotency_keys WHERE tenant_id = :t AND scope = :s AND key = :k FOR UPDATE"""
                ),
                {"t": tenant_id, "s": scope, "k": key, "lease": LEASE_SECONDS},
            )
        ).one()
        if row.request_hash != request_hash:
            raise IdempotencyConflict(
                "idempotency key was already used with a different request", details={"scope": scope}
            )
        if row.status == "completed":
            response = row.response
            if isinstance(response, dict):
                response = {**response, "replayed": True}
            return response
        if row.stale:
            await session.execute(
                text("UPDATE idempotency_keys SET updated_at = now() WHERE tenant_id = :t AND scope = :s AND key = :k"),
                {"t": tenant_id, "s": scope, "k": key},
            )
            return None
        raise Conflict("a request with this idempotency key is still in progress", details={"retry_after_s": 2})


async def complete(tenant_id: str, scope: str, key: str, response: Any) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                """UPDATE idempotency_keys SET status = 'completed', response = CAST(:r AS JSONB)
                   WHERE tenant_id = :t AND scope = :s AND key = :k"""
            ),
            {"t": tenant_id, "s": scope, "k": key, "r": json.dumps(response, default=str)},
        )


async def abandon(tenant_id: str, scope: str, key: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "DELETE FROM idempotency_keys WHERE tenant_id = :t AND scope = :s AND key = :k AND status = 'in_progress'"
            ),
            {"t": tenant_id, "s": scope, "k": key},
        )
