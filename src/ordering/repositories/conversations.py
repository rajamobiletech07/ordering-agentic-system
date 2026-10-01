"""Conversation + message persistence (the user-visible transcript; graph state lives in checkpoints)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ordering.common.errors import NotFound, PermissionDenied


async def get_or_create(session: AsyncSession, conversation_id: UUID | None, tenant_id: str, user_id: str,
                        title: str) -> tuple[UUID, bool]:
    if conversation_id is None:
        cid = uuid4()
        await session.execute(text(
            "INSERT INTO conversations (conversation_id, tenant_id, user_id, title) VALUES (:c, :t, :u, :title)"),
            {"c": cid, "t": tenant_id, "u": user_id, "title": title[:80]})
        return cid, True
    row = (await session.execute(text("SELECT user_id, status FROM conversations WHERE conversation_id = :c"),
                                 {"c": conversation_id})).first()
    if row is None:  # RLS hides other tenants' conversations → indistinguishable from missing
        raise NotFound("conversation not found")
    if row.user_id != user_id:
        raise PermissionDenied("this conversation belongs to another user")
    if row.status == "closed":
        raise PermissionDenied("conversation is closed; start a new one")
    return conversation_id, False


async def assert_access(session: AsyncSession, conversation_id: UUID, user_id: str, allow_admin: bool = False) -> dict:
    row = (await session.execute(text(
        "SELECT conversation_id, user_id, title, status, created_at, updated_at FROM conversations WHERE conversation_id = :c"),
        {"c": conversation_id})).first()
    if row is None:
        raise NotFound("conversation not found")
    if row.user_id != user_id and not allow_admin:
        raise PermissionDenied("this conversation belongs to another user")
    return dict(row._mapping)


async def add_message(session: AsyncSession, *, tenant_id: str, conversation_id: UUID, role: str, content: str,
                      intent: str | None = None, trace_id: str | None = None, request_id: str | None = None,
                      prompt_version: str | None = None, agent_version: str | None = None,
                      metadata: dict[str, Any] | None = None, message_id: UUID | None = None) -> UUID:
    mid = message_id or uuid4()
    await session.execute(text(
        """INSERT INTO messages (message_id, tenant_id, conversation_id, role, content, intent, trace_id, request_id,
               prompt_version, agent_version, metadata)
           VALUES (:m, :t, :c, :r, :content, :i, :tr, :rq, :pv, :av, CAST(:md AS JSONB))"""),
        {"m": mid, "t": tenant_id, "c": conversation_id, "r": role, "content": content, "i": intent, "tr": trace_id,
         "rq": request_id, "pv": prompt_version, "av": agent_version, "md": json.dumps(metadata or {}, default=str)})
    await session.execute(text("UPDATE conversations SET updated_at = now() WHERE conversation_id = :c"),
                          {"c": conversation_id})
    return mid


async def list_messages(session: AsyncSession, conversation_id: UUID, limit: int = 200) -> list[dict]:
    rows = await session.execute(text(
        """SELECT message_id, role, content, intent, created_at FROM messages WHERE conversation_id = :c
           ORDER BY created_at, role DESC LIMIT :l"""), {"c": conversation_id, "l": limit})
    return [dict(r._mapping) for r in rows]


async def get_message(session: AsyncSession, message_id: UUID) -> dict | None:
    row = (await session.execute(text(
        "SELECT message_id, conversation_id, role, content, trace_id, prompt_version, agent_version, metadata "
        "FROM messages WHERE message_id = :m"), {"m": message_id})).first()
    return dict(row._mapping) if row else None


async def close(session: AsyncSession, conversation_id: UUID) -> None:
    await session.execute(text("UPDATE conversations SET status = 'closed' WHERE conversation_id = :c"),
                          {"c": conversation_id})


async def user_conversation_ids(session: AsyncSession, user_id: str) -> list[str]:
    rows = await session.execute(text("SELECT conversation_id::text AS c FROM conversations WHERE user_id = :u"),
                                 {"u": user_id})
    return [r.c for r in rows]
