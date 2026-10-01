"""Append-only audit trail (who did what, as which agent, with what outcome)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from ordering.common.config import get_settings
from ordering.common.context import get_context
from ordering.common.db import tenant_session
from ordering.common.redaction import redact
from ordering.common.tracing import current_trace_id


async def record_audit_event(
    action: str,
    outcome: str = "success",
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> None:
    ctx = get_context()
    tenant = tenant_id or ctx.tenant_id
    async with tenant_session(tenant or "") as session:
        await session.execute(
            text(
                """INSERT INTO audit_events (tenant_id, actor_user_id, actor_agent, action, resource_id, outcome,
                       request_id, trace_id, agent_version, details)
                   VALUES (:tenant, :user, :agent, :action, :resource, :outcome, :rid, :tid, :ver, CAST(:details AS JSONB))"""
            ),
            {
                "tenant": tenant,
                "user": ctx.user_id,
                "agent": ctx.agent,
                "action": action,
                "resource": resource_id,
                "outcome": outcome,
                "rid": ctx.request_id,
                "tid": current_trace_id(),
                "ver": get_settings().agent_version,
                "details": json.dumps(redact(details or {}), default=str),
            },
        )


async def list_audit_events(tenant_id: str, limit: int = 50, action: str | None = None) -> list[dict[str, Any]]:
    async with tenant_session(tenant_id) as session:
        rows = await session.execute(
            text(
                """SELECT event_id, actor_user_id, actor_agent, action, resource_id, outcome, request_id, trace_id,
                          agent_version, details, created_at
                   FROM audit_events WHERE (CAST(:action AS TEXT) IS NULL OR action = :action)
                   ORDER BY created_at DESC LIMIT :limit"""
            ),
            {"action": action, "limit": limit},
        )
        return [dict(r._mapping) for r in rows]
