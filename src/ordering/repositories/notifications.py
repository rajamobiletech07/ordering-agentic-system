from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ordering.common.errors import NotFound
from ordering.domain.schemas.notification import Notification

_COLS = """notification_id, order_id, channel, recipient, template, status, attempts, max_attempts, last_error,
           delivered_at, created_at"""


async def find_by_idempotency_key(session: AsyncSession, key: str) -> Notification | None:
    row = (await session.execute(text(f"SELECT {_COLS} FROM notifications WHERE idempotency_key = :k"), {"k": key})).first()
    return Notification.model_validate(dict(row._mapping)) if row else None


async def enqueue(session: AsyncSession, *, tenant_id: str, order_id: UUID | None, channel: str, recipient: str,
                  template: str, payload: dict[str, Any], idempotency_key: str, max_attempts: int) -> Notification:
    nid = uuid4()
    envelope = {  # CloudEvents 1.0 structured envelope
        "specversion": "1.0",
        "id": str(nid),
        "source": f"/tenants/{tenant_id}/ordering",
        "type": f"com.ordering.{template}",
        "subject": str(order_id) if order_id else None,
        "datacontenttype": "application/json",
        "data": payload,
    }
    row = (
        await session.execute(
            text(
                f"""INSERT INTO notifications (notification_id, tenant_id, order_id, channel, recipient, template,
                        payload, idempotency_key, max_attempts)
                    VALUES (:id, :t, :o, :ch, :r, :tpl, CAST(:p AS JSONB), :key, :max) RETURNING {_COLS}"""
            ),
            {"id": nid, "t": tenant_id, "o": order_id, "ch": channel, "r": recipient, "tpl": template,
             "p": json.dumps(envelope, default=str), "key": idempotency_key, "max": max_attempts},
        )
    ).one()
    return Notification.model_validate(dict(row._mapping))


async def get(session: AsyncSession, notification_id: UUID) -> Notification:
    row = (await session.execute(text(f"SELECT {_COLS} FROM notifications WHERE notification_id = :id"),
                                 {"id": notification_id})).first()
    if row is None:
        raise NotFound("notification not found")
    return Notification.model_validate(dict(row._mapping))


async def list_for_order(session: AsyncSession, order_id: UUID) -> list[Notification]:
    rows = await session.execute(
        text(f"SELECT {_COLS} FROM notifications WHERE order_id = :o ORDER BY created_at"), {"o": order_id}
    )
    return [Notification.model_validate(dict(r._mapping)) for r in rows]


async def user_email(session: AsyncSession, user_id: str) -> str | None:
    row = (await session.execute(text("SELECT email FROM users WHERE user_id = :u"), {"u": user_id})).first()
    return row.email if row else None
