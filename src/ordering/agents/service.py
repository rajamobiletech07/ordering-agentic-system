"""Agent service (FastAPI, port 8010): hosts the LangGraph supervisor and both business agents.

Only the API gateway calls this service. The user's JWT is forwarded and re-verified here
(zero trust between services). Per-thread Redis locks serialize turns; chat POSTs honour the
``Idempotency-Key`` header so a client retry never re-runs a turn (e.g. never re-confirms an order).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request
from langgraph.types import Command

from ordering.agentops.feedback import record_feedback, record_signal
from ordering.agents.cache import ThreadCache, clear_user_threads, sweep_orphans
from ordering.agents.mcp_client import get_mcp_client
from ordering.agents.supervisor.graph import build_graph
from ordering.common.config import get_settings
from ordering.common.context import Principal, update_context
from ordering.common.db import dispose_engines, psycopg_dsn, tenant_session
from ordering.common.errors import Conflict, NotFound, OrderingError, PermissionDenied
from ordering.common.http import close_http_client
from ordering.common.ids import sha256_hex
from ordering.common.logging import configure_logging, get_logger
from ordering.common.metrics import metrics_response
from ordering.common.redis import close_redis, get_redis
from ordering.common.tracing import current_trace_id, setup_tracing
from ordering.domain.schemas.approval import ApprovalDecision
from ordering.domain.schemas.chat import (
    ChatRequest,
    ChatResponse,
    Conversation,
    ConversationMessage,
    FeedbackRequest,
)
from ordering.memory import long_term
from ordering.repositories import approvals as approvals_repo
from ordering.repositories import audit as audit_repo
from ordering.repositories import conversations as conv_repo
from ordering.repositories import idempotency as idem_repo
from ordering.security.dependencies import require_permissions
from ordering.security.permissions import (
    AUDIT_READ,
    CACHE_ADMIN,
    CHAT,
    FEEDBACK_WRITE,
    MEMORY_MANAGE,
    ORD_READ,
)
from ordering.web import install_error_handlers, install_request_context

log = get_logger("ordering.agent_service")
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _sweeper(stop: asyncio.Event) -> None:
    interval = get_settings().cache_sweep_interval_s
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await sweep_orphans()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    stop = asyncio.Event()
    if getattr(app.state, "graph", None) is None:  # tests may inject a graph with an in-memory saver
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # Create checkpoint tables with the owner role, then run with the least-privilege role.
        # On Kubernetes the db-migrate job does this (scripts.setup_checkpointer), so the service
        # never holds owner credentials.
        if settings.checkpointer_auto_setup:
            async with AsyncPostgresSaver.from_conn_string(psycopg_dsn(settings.secret("database_admin_url"))) as admin_saver:
                await admin_saver.setup()
        async with AsyncPostgresSaver.from_conn_string(psycopg_dsn(settings.secret("database_url"))) as saver:
            app.state.graph = build_graph(saver)
            sweeper = asyncio.create_task(_sweeper(stop))
            yield
            stop.set()
            sweeper.cancel()
    else:
        yield
    for task in list(_background):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)
    await close_http_client()
    await close_redis()
    await dispose_engines()


settings = get_settings()
settings.service_name = "agent-service"
configure_logging("agent-service", settings.log_level)
app = FastAPI(title="Agent Service (internal)", version=settings.agent_version, lifespan=lifespan)
install_request_context(app, "agent-service")
install_error_handlers(app)
setup_tracing(settings, app=app)


# ------------------------------------------------------------------------------------ helpers
@contextlib.asynccontextmanager
async def thread_lock(conversation_id: str, ttl_s: int = 120):
    key, token = f"lock:thread:{conversation_id}", uuid4().hex
    if not await get_redis().set(key, token, nx=True, ex=ttl_s):
        raise Conflict("another turn is still being processed for this conversation", details={"retry_after_s": 2})
    try:
        yield
    finally:
        # compare-and-delete so we never release someone else's lock
        await get_redis().eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end "
                               "return 0", 1, key, token)


def _config(conversation_id: str) -> dict:
    return {"configurable": {"thread_id": conversation_id}, "recursion_limit": 40}


async def _interrupted(graph, conversation_id: str) -> bool:
    snapshot = await graph.aget_state(_config(conversation_id))
    return any(task.interrupts for task in snapshot.tasks)


def _to_chat_response(conversation_id: UUID, message_id: UUID, response: dict[str, Any]) -> ChatResponse:
    return ChatResponse(
        conversation_id=conversation_id, message_id=message_id, reply=response.get("reply", ""),
        intent=response.get("intent"), status=response.get("status", "completed"),
        pending_approval=response.get("pending_approval"), citations=response.get("citations", []),
        data=response.get("data", {}), trace_id=current_trace_id(), agent_version=get_settings().agent_version,
    )


async def _run_turn(graph, principal: Principal, conversation_id: UUID, message: str | None,
                    resume: dict[str, Any] | None = None) -> dict[str, Any]:
    cid = str(conversation_id)
    update_context(principal=principal, conversation_id=cid)
    identity = {"conversation_id": cid, "tenant_id": principal.tenant_id, "user_id": principal.user_id,
                "roles": sorted(principal.roles)}
    config = _config(cid)
    if resume is not None:
        result = await graph.ainvoke(Command(resume=resume), config)
    elif message is not None and await _interrupted(graph, cid):
        # A new message while an approval is pending: resume the gate with the message; the graph
        # classifies it (yes/no → decision, anything else → answered, approval kept pending).
        result = await graph.ainvoke(Command(resume={"decision": "deferred"},
                                             update={"user_message": message, "response": {}, "intent": {}}), config)
    else:
        result = await graph.ainvoke({**identity, "user_message": message or "", "response": {}, "intent": {}}, config)
    return result


async def _persist_and_signal(principal: Principal, conversation_id: UUID, message_id: UUID, user_message: str | None,
                              state: dict[str, Any]) -> None:
    response = state.get("response", {})
    intent = state.get("intent") or {}
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        if user_message is not None:
            await conv_repo.add_message(s, tenant_id=principal.tenant_id, conversation_id=conversation_id, role="user",
                                        content=user_message, request_id=None)
        await conv_repo.add_message(
            s, tenant_id=principal.tenant_id, conversation_id=conversation_id, role="assistant",
            content=response.get("reply", ""), intent=intent.get("intent"), trace_id=current_trace_id(),
            prompt_version=intent.get("prompt"), agent_version=get_settings().agent_version, message_id=message_id,
            metadata={"status": response.get("status"), "citations": response.get("citations", []),
                      "llm_provider": intent.get("llm_provider"), "degraded": intent.get("degraded")})
    # implicit feedback-loop signals
    signal = None
    if response.get("status") == "refused":
        signal = ("guardrail", ["blocked_input"])
    elif response.get("evidence") == "insufficient":
        signal = ("refusal", ["insufficient_evidence"])
    elif intent.get("degraded") or response.get("status") == "degraded":
        signal = ("fallback", ["degraded_response"])
    if signal and user_message:
        _spawn(record_signal(principal, signal[0], str(conversation_id), message_id, current_trace_id(), user_message,
                             response.get("reply"), signal[1]))
    if user_message and response.get("status") not in ("refused",):
        _spawn(long_term.extract_and_store(principal, user_message, response.get("reply", ""), str(conversation_id)))


# ------------------------------------------------------------------------------------ routes
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    from ordering.common.db import ping

    await ping()
    await get_redis().ping()
    return {"status": "ready", "mcp_circuits": {k: b.state for k, b in get_mcp_client().breakers.items()}}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


@app.post("/internal/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request, principal: Principal = Depends(require_permissions(CHAT)),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    graph = request.app.state.graph
    scope = "api.chat"
    if idempotency_key:
        replay = await idem_repo.begin(principal.tenant_id, scope, idempotency_key,
                                       sha256_hex({"m": body.message, "c": str(body.conversation_id)}))
        if replay is not None:
            return ChatResponse(**replay)
    try:
        async with tenant_session(principal.tenant_id, principal.user_id) as s:
            conversation_id, _ = await conv_repo.get_or_create(s, body.conversation_id, principal.tenant_id,
                                                               principal.user_id, body.message)
        message_id = uuid4()
        started = time.perf_counter()
        async with thread_lock(str(conversation_id)):
            try:
                state = await _run_turn(graph, principal, conversation_id, body.message)
            except OrderingError:
                raise
            except Exception:
                log.exception("turn_failed", conversation_id=str(conversation_id))
                state = {"response": {"reply": "Something went wrong while processing your request. Your progress is "
                                               "saved - try again, or call POST /v1/conversations/{id}/resume.",
                                      "status": "error"}}
        await _persist_and_signal(principal, conversation_id, message_id, body.message, state)
        response = _to_chat_response(conversation_id, message_id, state.get("response", {}))
        log.info("chat_turn", intent=response.intent, status=response.status,
                 ms=round((time.perf_counter() - started) * 1000))
    except BaseException:
        if idempotency_key:
            await idem_repo.abandon(principal.tenant_id, scope, idempotency_key)
        raise
    if idempotency_key:
        await idem_repo.complete(principal.tenant_id, scope, idempotency_key, response.model_dump(mode="json"))
    return response


@app.post("/internal/approvals/{approval_id}", response_model=ChatResponse)
async def decide_approval(approval_id: UUID, body: ApprovalDecision, request: Request,
                          principal: Principal = Depends(require_permissions(CHAT))):
    """Approve/reject via API (e.g. an approver's inbox). Resumes the paused workflow."""
    graph = request.app.state.graph
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        approval = await approvals_repo.get(s, approval_id)
    if approval.conversation_id is None:
        raise NotFound("approval is not linked to a conversation")
    conversation_id = approval.conversation_id
    message_id = uuid4()
    async with thread_lock(str(conversation_id)):
        snapshot = await graph.aget_state(_config(str(conversation_id)))
        pending = snapshot.values.get("pending_approval") or {}
        if not any(t.interrupts for t in snapshot.tasks) or pending.get("approval_id") != str(approval_id):
            raise Conflict("the conversation is not waiting for this approval",
                           details={"approval_status": approval.status})
        # Role / four-eyes / expiry rules are enforced by the repository with the *decider's* identity.
        async with tenant_session(principal.tenant_id, principal.user_id) as s:
            await approvals_repo.decide(s, approval_id, principal, body.decision == "approve", body.comment)
        requester = Principal(user_id=approval.requested_by, tenant_id=principal.tenant_id,
                              roles=frozenset(snapshot.values.get("roles", [])))
        state = await _run_turn(graph, requester, conversation_id, None,
                                resume={"decision": body.decision, "decided_by": principal.user_id})
    await _persist_and_signal(requester, conversation_id, message_id, None, state)
    return _to_chat_response(conversation_id, message_id, state.get("response", {}))


@app.get("/internal/approvals/{approval_id}")
async def get_approval(approval_id: UUID, principal: Principal = Depends(require_permissions(CHAT))):
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        return (await approvals_repo.get(s, approval_id)).model_dump(mode="json")


@app.get("/internal/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: UUID, principal: Principal = Depends(require_permissions(CHAT))):
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        conv = await conv_repo.assert_access(s, conversation_id, principal.user_id, principal.has_role("admin"))
        messages = await conv_repo.list_messages(s, conversation_id)
        pending = await approvals_repo.pending_for_conversation(s, conversation_id)
    return Conversation(
        conversation_id=conversation_id, title=conv["title"], status=conv["status"], created_at=conv["created_at"],
        updated_at=conv["updated_at"], messages=[ConversationMessage(**m) for m in messages],
        pending_approval=({"approval_id": pending.approval_id, "action": pending.action, "summary": pending.summary,
                           "required_role": pending.required_role, "expires_at": pending.expires_at,
                           "payload": pending.payload} if pending else None),
    )


@app.post("/internal/conversations/{conversation_id}/resume", response_model=ChatResponse)
async def resume_conversation(conversation_id: UUID, request: Request,
                              principal: Principal = Depends(require_permissions(CHAT))):
    """Recovery: continue a turn that crashed mid-graph from its last checkpoint."""
    graph = request.app.state.graph
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        await conv_repo.assert_access(s, conversation_id, principal.user_id)
    async with thread_lock(str(conversation_id)):
        snapshot = await graph.aget_state(_config(str(conversation_id)))
        if not snapshot.next or any(t.interrupts for t in snapshot.tasks):
            state = snapshot.values
            response = {**(state.get("response") or {}), "reply": "Nothing to resume - the conversation is up to date."
                        + (" A proposal is waiting for approval." if state.get("pending_approval") else "")}
            return _to_chat_response(conversation_id, uuid4(), response)
        update_context(principal=principal, conversation_id=str(conversation_id))
        state = await graph.ainvoke(None, _config(str(conversation_id)))
    message_id = uuid4()
    await _persist_and_signal(principal, conversation_id, message_id, None, state)
    return _to_chat_response(conversation_id, message_id, state.get("response", {}))


@app.post("/internal/conversations/{conversation_id}/close")
async def close_conversation(conversation_id: UUID, principal: Principal = Depends(require_permissions(CHAT))):
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        await conv_repo.assert_access(s, conversation_id, principal.user_id)
        await conv_repo.close(s, conversation_id)
        await approvals_repo.expire_pending(s, conversation_id)
    removed = await ThreadCache(principal.tenant_id, str(conversation_id)).clear("conversation_closed")
    await audit_repo.record_audit_event("conversation.close", resource_id=str(conversation_id),
                                        details={"cache_keys_removed": removed})
    return {"conversation_id": str(conversation_id), "status": "closed", "cache_keys_removed": removed}


@app.delete("/internal/conversations/{conversation_id}/cache")
async def clear_cache(conversation_id: UUID, principal: Principal = Depends(require_permissions(CHAT))):
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        await conv_repo.assert_access(s, conversation_id, principal.user_id,
                                      allow_admin=principal.has_role("admin"))
    removed = await ThreadCache(principal.tenant_id, str(conversation_id)).clear("explicit_request")
    await audit_repo.record_audit_event("cache.clear", resource_id=str(conversation_id), details={"keys": removed})
    return {"conversation_id": str(conversation_id), "cache_keys_removed": removed}


@app.get("/internal/conversations/{conversation_id}/cache")
async def cache_stats(conversation_id: UUID, principal: Principal = Depends(require_permissions(CHAT))):
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        await conv_repo.assert_access(s, conversation_id, principal.user_id, allow_admin=principal.has_role("admin"))
    cache = ThreadCache(principal.tenant_id, str(conversation_id))
    return {"conversation_id": str(conversation_id), "keys": await cache.size(), "ttl_s": cache.ttl}


@app.post("/internal/conversations/{conversation_id}/feedback", status_code=201)
async def feedback(conversation_id: UUID, body: FeedbackRequest,
                   principal: Principal = Depends(require_permissions(FEEDBACK_WRITE))):
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        await conv_repo.assert_access(s, conversation_id, principal.user_id)
        message = await conv_repo.get_message(s, body.message_id) if body.message_id else None
        if body.message_id and (message is None or message["conversation_id"] != conversation_id):
            raise NotFound("message not found in this conversation")
        user_message = None
        if message:
            msgs = await conv_repo.list_messages(s, conversation_id)
            idx = next((i for i, m in enumerate(msgs) if m["message_id"] == message["message_id"]), None)
            user_message = next((m["content"] for m in reversed(msgs[:idx]) if m["role"] == "user"), None) if idx else None
    fid = await record_feedback(principal, conversation_id, message, body.rating, body.labels, body.comment, user_message)
    return {"feedback_id": str(fid), "status": "recorded"}


@app.get("/internal/memory")
async def get_memory(principal: Principal = Depends(require_permissions(MEMORY_MANAGE))):
    return {"memories": await long_term.list_memories(principal)}


@app.delete("/internal/memory/{memory_id}")
async def delete_memory(memory_id: UUID, principal: Principal = Depends(require_permissions(MEMORY_MANAGE))):
    if not await long_term.delete_memory(principal, memory_id):
        raise NotFound("memory not found")
    return {"deleted": str(memory_id)}


@app.delete("/internal/memory")
async def erase_memory(principal: Principal = Depends(require_permissions(MEMORY_MANAGE))):
    """Right to erasure: all long-term memories + every thread cache of this user."""
    erased = await long_term.erase_all(principal)
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        threads = await conv_repo.user_conversation_ids(s, principal.user_id)
    cleared = await clear_user_threads(principal.tenant_id, threads, "memory_erased")
    await audit_repo.record_audit_event("memory.erase", details={"memories": erased, "cache_keys": cleared})
    return {"memories_erased": erased, "cache_keys_removed": cleared}


@app.get("/internal/orders/{order_id}")
async def get_order(order_id: UUID, principal: Principal = Depends(require_permissions(ORD_READ))):
    update_context(principal=principal, agent="ordering_agent")
    order = await get_mcp_client().call("ordering.get_order", {"order_id": str(order_id)})
    try:
        payment = await get_mcp_client().call("payment.get_payment_status", {"order_id": str(order_id)})
        notifications = (await get_mcp_client().call("notification.list_notifications",
                                                     {"order_id": str(order_id)}))["notifications"]
    except OrderingError:
        payment, notifications = None, []
    return {"order": order, "payment": payment, "notifications": notifications}


@app.get("/internal/audit")
async def audit(limit: int = 50, action: str | None = None, principal: Principal = Depends(require_permissions(AUDIT_READ))):
    return {"events": await audit_repo.list_audit_events(principal.tenant_id, min(limit, 500), action)}


@app.post("/internal/dev/faults")
async def dev_faults(body: dict, principal: Principal = Depends(require_permissions(CACHE_ADMIN))):
    """Admin-only, non-production fault injection for resilience demos."""
    from ordering.common.faults import set_fault

    if get_settings().env == "production":
        raise PermissionDenied("fault injection is disabled in production")
    await set_fault(body["target"], body.get("mode"), int(body.get("ttl_s", 600)))
    await audit_repo.record_audit_event("dev.fault_injection", details=body)
    return {"target": body["target"], "mode": body.get("mode")}
