"""Long-term (cross-conversation) memory.

Adapted from mcp-core ``memory_server/memory/core.py`` (LLM fact extraction → compare with
similar existing memories → ADD / UPDATE / DELETE / NONE, with a history table), rebuilt on the
shared Postgres (pgvector) with tenant RLS, PII redaction, deny-listed content, retention, and
a right-to-erasure API.

Rules: memories hold *preferences and working facts*, never transactional truth (order and
payment status always come from MCP tools). Extraction runs after the turn, off the request path.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import text

from ordering.common.config import get_settings
from ordering.common.context import Principal
from ordering.common.db import tenant_session
from ordering.common.ids import sha256_hex
from ordering.common.logging import get_logger
from ordering.common.redaction import redact_text
from ordering.governance.policy import get_policy
from ordering.llm_client.client import get_llm_client
from ordering.prompts import registry
from ordering.rag.embeddings.base import get_embedder

log = get_logger("ordering.memory")


class MemoryOp(BaseModel):
    event: Literal["ADD", "UPDATE", "DELETE", "NONE"]
    memory_id: str | None = None
    content: str = Field("", max_length=300)
    memory_type: Literal["preference", "fact", "procedural"] = "preference"


class MemoryOps(BaseModel):
    operations: list[MemoryOp] = Field(default_factory=list)


_RULES = [
    (re.compile(r"\b(?:i prefer|always|by default)\b.{0,30}\b(?:pay|paying|payment)\b.{0,20}\b(tok_[a-z_]+)", re.I),
     lambda m: f"Prefers to pay with {m.group(1)}"),
    (re.compile(r"\bmy (?:default|home|usual) (?:store|location|dc) is (STORE-G?\d{2,3}|DC-G?\d{2})", re.I),
     lambda m: f"Default location is {m.group(1).upper()}"),
    (re.compile(r"\b(?:notify|message|alert) me (?:by|via|on) (email|sms|in-app|in app)", re.I),
     lambda m: f"Prefers notifications via {m.group(1).lower().replace(' ', '_').replace('-', '_')}"),
    (re.compile(r"\b(?:i|we) (?:prefer|like) (?:vendor|supplier) ([A-Za-z0-9 &-]{3,40}?)(?:\.|,|$)", re.I),
     lambda m: f"Prefers vendor {m.group(1).strip()}"),
]
_FORGET = re.compile(r"\b(forget|stop remembering|don'?t remember)\b", re.I)


def rule_based_ops(user_message: str, existing: list[dict]) -> MemoryOps:
    ops: list[MemoryOp] = []
    for pattern, render in _RULES:
        if m := pattern.search(user_message):
            content = render(m)
            prefix = content.split(" ", 3)[:3]
            same_kind = next((e for e in existing if e["content"].split(" ", 3)[:3] == prefix), None)
            if same_kind and same_kind["content"] != content:
                ops.append(MemoryOp(event="UPDATE", memory_id=same_kind["memory_id"], content=content))
            elif not same_kind:
                ops.append(MemoryOp(event="ADD", content=content))
    if _FORGET.search(user_message) and existing:
        for e in existing:
            if any(w.lower() in e["content"].lower() for w in re.findall(r"[A-Za-z0-9_-]{4,}", user_message)):
                ops.append(MemoryOp(event="DELETE", memory_id=e["memory_id"]))
    return MemoryOps(operations=ops)


def _allowed(content: str) -> bool:
    lowered = content.lower()
    return content.strip() != "" and not any(p in lowered for p in get_policy().memory.deny_patterns)


async def recall(principal: Principal, query: str, limit: int = 5) -> list[dict]:
    profile = get_settings().embedding_profile
    qvec = None
    try:
        qvec = (await get_embedder(profile).embed([query]))[0]
    except Exception as exc:  # embedding provider down → recency-ordered recall
        log.debug("memory_recall_embedding_failed", error=str(exc)[:100])
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        rows = await s.execute(text(
            """SELECT memory_id::text AS memory_id, content, memory_type, confidence, updated_at
               FROM memories
               WHERE (user_id = :u OR scope = 'tenant') AND (expires_at IS NULL OR expires_at > now())
               ORDER BY CASE WHEN CAST(:q AS vector) IS NOT NULL AND embedding_profile = :p AND embedding IS NOT NULL
                             THEN embedding <=> CAST(:q AS vector) ELSE 1 END, updated_at DESC
               LIMIT :k"""),
            {"u": principal.user_id, "q": json.dumps(qvec) if qvec else None, "p": profile, "k": limit})
        return [dict(r._mapping) for r in rows]


async def list_memories(principal: Principal) -> list[dict]:
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        rows = await s.execute(text(
            """SELECT memory_id::text AS memory_id, scope, memory_type, content, confidence, source_conversation_id,
                      created_at, updated_at, expires_at FROM memories WHERE user_id = :u ORDER BY updated_at DESC"""),
            {"u": principal.user_id})
        return [dict(r._mapping) for r in rows]


async def _history(s, memory_id, principal, event, old, new) -> None:
    await s.execute(text(
        """INSERT INTO memory_history (memory_id, tenant_id, user_id, event, old_content, new_content)
           VALUES (:m, :t, :u, :e, :o, :n)"""),
        {"m": memory_id, "t": principal.tenant_id, "u": principal.user_id, "e": event, "o": old, "n": new})


async def apply_ops(principal: Principal, ops: MemoryOps, conversation_id: str | None) -> list[str]:
    settings = get_settings()
    applied: list[str] = []
    profile = settings.embedding_profile
    expires = datetime.now(UTC) + timedelta(days=get_policy().memory.retention_days)
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        for op in ops.operations:
            content = redact_text(op.content.strip())[:300]
            if op.event in ("ADD", "UPDATE") and not _allowed(content):
                continue
            vector = None
            if op.event in ("ADD", "UPDATE"):
                try:
                    vector = json.dumps((await get_embedder(profile).embed([content]))[0])
                except Exception:
                    vector = None
            if op.event == "ADD":
                memory_id = uuid4()
                result = await s.execute(text(
                    """INSERT INTO memories (memory_id, tenant_id, user_id, scope, memory_type, content, content_sha256,
                           source_conversation_id, embedding_profile, embedding, expires_at)
                       VALUES (:id, :t, :u, 'user', :mt, :c, :h, :cid, :p, CAST(:e AS vector), :exp)
                       ON CONFLICT DO NOTHING RETURNING memory_id"""),
                    {"id": memory_id, "t": principal.tenant_id, "u": principal.user_id, "mt": op.memory_type, "c": content,
                     "h": sha256_hex(content.lower()), "cid": conversation_id, "p": profile, "e": vector, "exp": expires})
                if result.first():
                    await _history(s, memory_id, principal, "ADD", None, content)
                    applied.append(f"ADD {content}")
            elif op.event == "UPDATE" and op.memory_id:
                old = (await s.execute(text("SELECT content FROM memories WHERE memory_id = :id AND user_id = :u"),
                                       {"id": UUID(op.memory_id), "u": principal.user_id})).first()
                if old:
                    await s.execute(text(
                        """UPDATE memories SET content = :c, content_sha256 = :h, embedding = CAST(:e AS vector),
                               embedding_profile = :p, expires_at = :exp WHERE memory_id = :id"""),
                        {"c": content, "h": sha256_hex(content.lower()), "e": vector, "p": profile, "exp": expires,
                         "id": UUID(op.memory_id)})
                    await _history(s, UUID(op.memory_id), principal, "UPDATE", old.content, content)
                    applied.append(f"UPDATE {content}")
            elif op.event == "DELETE" and op.memory_id:
                old = (await s.execute(text("DELETE FROM memories WHERE memory_id = :id AND user_id = :u RETURNING content"),
                                       {"id": UUID(op.memory_id), "u": principal.user_id})).first()
                if old:
                    await _history(s, UUID(op.memory_id), principal, "DELETE", old.content, None)
                    applied.append(f"DELETE {old.content}")
    return applied


async def extract_and_store(principal: Principal, user_message: str, assistant_message: str,
                            conversation_id: str | None) -> list[str]:
    """Post-turn extraction (LLM with deterministic fallback)."""
    if not get_settings().long_term_memory_enabled:
        return []
    try:
        existing = await recall(principal, user_message, limit=10)
        fallback = rule_based_ops(user_message, existing)
        prompt = registry.get("memory_extractor")
        system, user = prompt.render(existing=json.dumps([{"id": e["memory_id"], "content": e["content"]} for e in existing]),
                                     user_message=redact_text(user_message)[:1500],
                                     assistant_message=redact_text(assistant_message)[:1500])
        result = await get_llm_client().complete(prompt.route, system=system, user=user, schema=MemoryOps,
                                                 fallback=fallback, prompt_id=prompt.prompt_id,
                                                 prompt_version=prompt.version)
        applied = await apply_ops(principal, result.parsed, conversation_id)
        if applied:
            log.info("long_term_memory_updated", ops=applied)
        return applied
    except Exception as exc:  # never affect the user-facing turn
        log.warning("memory_extraction_failed", error=str(exc)[:200])
        return []


async def delete_memory(principal: Principal, memory_id: UUID) -> bool:
    ops = MemoryOps(operations=[MemoryOp(event="DELETE", memory_id=str(memory_id))])
    return bool(await apply_ops(principal, ops, None))


async def erase_all(principal: Principal) -> int:
    """Right to erasure: delete every memory of the user (history keeps an ERASE marker only)."""
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        rows = (await s.execute(text("DELETE FROM memories WHERE user_id = :u RETURNING memory_id"),
                                {"u": principal.user_id})).all()
        for r in rows:
            await _history(s, r.memory_id, principal, "ERASE", None, None)
    return len(rows)
