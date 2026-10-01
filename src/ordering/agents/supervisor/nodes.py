"""Supervisor graph nodes.

The supervisor understands intent, plans the workflow for that intent, delegates typed tasks to
the Recommendation and Ordering agents, maintains conversation state, handles failures, and
gates every consequential action behind a human approval (LangGraph ``interrupt``).
"""

from __future__ import annotations

import re
from typing import Any

from langgraph.types import interrupt

from ordering.agents.cache import ThreadCache
from ordering.agents.contracts import OrderProposal, PaymentSelection, TaskRequest, TaskResult
from ordering.agents.guardrails import screen_input
from ordering.agents.ordering.agent import OrderingAgent
from ordering.agents.recommendation.agent import RecommendationAgent
from ordering.agents.supervisor.intents import Intent, classify_rules
from ordering.common.config import get_settings
from ordering.common.context import Principal, get_context, update_context
from ordering.common.db import tenant_session
from ordering.common.errors import ApprovalRequired, PermissionDenied
from ordering.common.logging import get_logger
from ordering.common.metrics import AGENT_TURNS, APPROVALS
from ordering.governance.output_guardrails import check_reply
from ordering.governance.policy import get_policy
from ordering.llm_client.client import get_llm_client
from ordering.memory import long_term, short_term
from ordering.prompts import registry
from ordering.repositories import approvals as approvals_repo
from ordering.repositories import orders as orders_repo
from ordering.security.permissions import SUPERVISOR

log = get_logger("ordering.supervisor")

HELP = ("I can help you with replenishment ordering:\n"
        "- **Recommendations** - \"what should I order for STORE-014?\" or \"show critical recommendations\"\n"
        "- **Explanations** - \"why is SKU-10042 recommended?\"\n"
        "- **Risk** - \"which items are at risk of stock-out at DC-02?\"\n"
        "- **Policies** - \"what is the approval threshold?\"\n"
        "- **Orders** - \"order the first one\", \"status of AR-200001\", \"retry payment\", \"cancel order AR-200001\"")

RECOMMENDATION_INTENTS = {"get_recommendations", "explain_recommendation", "investigate_risk", "policy_question"}


def _cache(state) -> ThreadCache:
    return ThreadCache(state["tenant_id"], state["conversation_id"])


def _principal(state) -> Principal:
    """The requester, from the checkpointed identity snapshot."""
    ctx = get_context().principal
    if ctx and ctx.user_id == state["user_id"] and ctx.tenant_id == state["tenant_id"]:
        return ctx
    return Principal(user_id=state["user_id"], tenant_id=state["tenant_id"], roles=frozenset(state.get("roles", [])))


def _log_task(state, agent: str, task: TaskRequest, result: TaskResult) -> list[dict]:
    entry = {"task_id": str(task.task_id), "agent": agent, "task_type": task.task_type, "status": result.status,
             "errors": [e.get("code") for e in result.errors]}
    return [*(state.get("task_log") or [])[-49:], entry]


def _response(reply: str, status: str = "completed", **extra) -> dict[str, Any]:
    return {"reply": reply, "status": status, "citations": [], "data": {}, **extra}


# ---------------------------------------------------------------------------------------- nodes
async def guard(state) -> dict:
    update_context(agent=SUPERVISOR)
    screen = await screen_input(state["user_message"])
    if screen.action == "block":
        AGENT_TURNS.labels("blocked", "refused").inc()
        return {"blocked": True, "intent": {"intent": "blocked", "confidence": 1.0, "entities": {}},
                "response": _response(f"I can't help with that request. {screen.reason}", "refused",
                                      data={"guardrail": {"signals": screen.signals, "score": screen.score}})}
    return {"blocked": False, "response": {}}


async def prepare(state) -> dict:
    messages = [*(state.get("messages") or []), {"role": "user", "content": state["user_message"]}]
    window, summary, _ = await short_term.compact_window(messages, state.get("summary", ""), state["conversation_id"])
    memories: list[dict] = []
    if get_settings().long_term_memory_enabled:
        try:
            memories = await long_term.recall(_principal(state), state["user_message"], limit=5)
        except Exception as exc:  # memory is an enhancement, never a dependency
            log.warning("memory_recall_failed", error=str(exc)[:120])
    return {"messages": window, "summary": summary,
            "memories": [{"memory_id": m["memory_id"], "content": m["content"]} for m in memories]}


async def classify(state) -> dict:
    text = state["user_message"]
    pending = bool(state.get("pending_approval"))
    has_recs = bool(state.get("last_recommendations"))
    fallback = classify_rules(text, pending, has_recs)
    prompt = await registry.resolve("intent_classifier", state["conversation_id"])
    system, user = prompt.render(state=short_term.state_brief(state), message=text)
    result = await get_llm_client().complete(prompt.route, system=system, user=user, schema=Intent, fallback=fallback,
                                             prompt_id=prompt.prompt_id, prompt_version=prompt.version,
                                             cache=_cache(state))
    intent: Intent = result.parsed
    # Deterministic entity extraction wins for strict identifiers (never trust the model for IDs).
    rule_e = fallback.entities
    for field in ("sku", "location_code", "order_number", "payment_token"):
        if getattr(rule_e, field):
            setattr(intent.entities, field, getattr(rule_e, field))
    if intent.intent in ("confirm", "reject") and not pending:
        intent.intent = "unsupported"
    return {"intent": {**intent.model_dump(), "prompt": prompt.ref, "llm_provider": result.provider,
                       "degraded": result.degraded}}


def route_after_guard(state) -> str:
    return "finalize" if state.get("blocked") else "prepare"


def route_after_classify(state) -> str:
    intent = state["intent"]["intent"]
    if intent in RECOMMENDATION_INTENTS:
        return "recommendation"
    return {
        "propose_order": "propose",
        "confirm": "decide_in_chat",
        "reject": "decide_in_chat",
        "order_status": "order_status",
        "retry_payment": "prepare_retry",
        "cancel_order": "prepare_cancel",
    }.get(intent, "respond")


async def recommendation(state) -> dict:
    intent = state["intent"]
    entities = {k: v for k, v in intent.get("entities", {}).items() if v is not None}
    task = TaskRequest(task_type=intent["intent"], inputs={**entities, "question": state["user_message"]},
                       context_refs={"tenant_id": state["tenant_id"],
                                     "last_recommendations": state.get("last_recommendations") or [],
                                     "focus_recommendation_id": state.get("focus_recommendation_id")})
    result = await RecommendationAgent(_cache(state), state["conversation_id"]).handle(task)
    update: dict[str, Any] = {"task_log": _log_task(state, "recommendation_agent", task, result)}
    if result.data.get("recommendations"):
        update["last_recommendations"] = result.data["recommendations"]
        update["focus_recommendation_id"] = (result.data["recommendations"][0]["recommendation_id"]
                                             if len(result.data["recommendations"]) == 1 else None)
    if "focus_recommendation" in result.data:
        update["focus_recommendation_id"] = result.data["focus_recommendation"]["recommendation_id"]
    status = {"insufficient_evidence": "completed", "degraded": "degraded", "failed": "degraded"}.get(result.status, "completed")
    update["response"] = _response(result.message, status, citations=[c.model_dump(mode="json") for c in result.citations],
                                   data={k: v for k, v in result.data.items() if k != "focus_recommendation"},
                                   evidence="insufficient" if result.status == "insufficient_evidence" else None)
    return update


async def _tenant_threshold(state) -> str:
    async with tenant_session(state["tenant_id"], state["user_id"]) as s:
        limits = await orders_repo.tenant_limits(s, state["tenant_id"])
    return str(limits["approval_threshold"])


def _payment_choice(state, token: str | None) -> dict:
    if not token:
        for m in state.get("memories") or []:
            if found := re.search(r"\b(tok_[a-z_]+)\b", m["content"]):
                token = found.group(1)
                break
    if not token or token == "tok_visa":  # noqa: S105  # nosec B105 - simulated payment token, not a secret
        return PaymentSelection().model_dump()
    return PaymentSelection(payment_method_token=token, label=f"simulated payment method {token}").model_dump()


async def _open_approval(state, action: str, payload: dict, summary: str, required_role: str) -> dict:
    principal = _principal(state)
    async with tenant_session(state["tenant_id"], state["user_id"]) as s:
        if state.get("pending_approval"):
            await approvals_repo.expire_pending(s, state["conversation_id"])
        approval = await approvals_repo.create(s, principal=principal, conversation_id=state["conversation_id"],
                                               action=action, payload=payload, summary=summary,
                                               required_role=required_role,
                                               ttl_minutes=get_policy().autonomy.approval_ttl_minutes)
    return {"approval_id": str(approval.approval_id), "action": action, "summary": summary,
            "required_role": required_role, "expires_at": approval.expires_at.isoformat(), "payload": payload}


def _approval_prompt(pending: dict) -> str:
    who = ("an **approver** (a different user holding the approver role) must approve this high-value action"
           if pending["required_role"] == "approver" else "reply **yes** to confirm or **no** to cancel")
    return (f"**Please confirm:** {pending['summary']}\n\nNothing happens until you confirm - {who}. "
            f"(approval `{pending['approval_id']}`, expires {pending['expires_at'][:16].replace('T', ' ')} UTC)")


async def propose(state) -> dict:
    intent = state["intent"]
    entities = {k: v for k, v in intent.get("entities", {}).items() if v is not None}
    task = TaskRequest(task_type="build_proposal", inputs=entities,
                       context_refs={"tenant_id": state["tenant_id"],
                                     "last_recommendations": state.get("last_recommendations") or [],
                                     "focus_recommendation_id": state.get("focus_recommendation_id"),
                                     "approval_threshold": await _tenant_threshold(state),
                                     "payment": _payment_choice(state, entities.get("payment_token"))})
    result = await RecommendationAgent(_cache(state), state["conversation_id"]).handle(task)
    update: dict[str, Any] = {"task_log": _log_task(state, "recommendation_agent", task, result)}
    if result.status != "needs_approval" or not isinstance(result.proposed_action, OrderProposal):
        retryable = any(e.get("retryable") for e in result.errors)
        update["response"] = _response(result.message, "degraded" if retryable else "completed")
        return update
    proposal: OrderProposal = result.proposed_action
    pending = await _open_approval(state, "create_order", proposal.approval_payload(), proposal.summary(),
                                   proposal.requires_role)
    APPROVALS.labels("create_order", "requested").inc()
    update.update({
        "proposal": proposal.model_dump(mode="json"), "pending_approval": pending, "workflow": "awaiting_approval",
        "focus_recommendation_id": str(proposal.recommendation_id),
        "response": _response(_approval_prompt(pending) + f"\n\nWhy: {proposal.rationale}", "awaiting_approval",
                              pending_approval=pending),
    })
    return update


async def approval_gate(state) -> dict:
    """Pause the workflow until a human decides. Resumed by the API (approve/reject endpoint)
    or by the next chat message (``deferred`` → classified; "yes"/"no" become decisions)."""
    pending = state["pending_approval"]
    decision = interrupt({"type": "approval_required", "approval_id": pending["approval_id"],
                          "action": pending["action"], "summary": pending["summary"],
                          "required_role": pending["required_role"]})
    return {"approval_decision": decision}


def route_after_gate(state) -> str:
    decision = (state.get("approval_decision") or {}).get("decision")
    return {"approve": "execute", "reject": "rejected", "expired": "rejected"}.get(decision, "guard")


async def decide_in_chat(state) -> dict:
    """User answered the pending approval in chat. The decision is recorded in the DB with the
    *chatting* user's identity - role and four-eyes rules are enforced by the repository."""
    pending = state.get("pending_approval")
    intent = state["intent"]
    approve = intent["intent"] == "confirm"
    new_token = (intent.get("entities") or {}).get("payment_token")
    if approve and pending and pending["action"] == "create_order" and new_token \
            and new_token != pending["payload"]["payment"]["payment_method_token"]:
        # Approved parameters are immutable: a changed payment method needs a fresh approval.
        payload = {**pending["payload"], "payment": {"method": "card", "payment_method_token": new_token}}
        summary = re.sub(r"pay with .*\.$", f"pay with simulated payment method {new_token}.", pending["summary"])
        new_pending = await _open_approval(state, "create_order", payload, summary, pending["required_role"])
        return {"pending_approval": new_pending, "approval_decision": None,
                "response": _response("You changed the payment method, so here is the updated proposal.\n\n"
                                      + _approval_prompt(new_pending), "awaiting_approval", pending_approval=new_pending)}
    principal = get_context().principal or _principal(state)
    try:
        async with tenant_session(state["tenant_id"], principal.user_id) as s:
            await approvals_repo.decide(s, pending["approval_id"], principal, approve, "decided in chat")
    except PermissionDenied as exc:
        return {"approval_decision": None,
                "response": _response(f"I can't record your confirmation: {exc.message}. The proposal stays pending - "
                                      "an approver can approve it via `POST /v1/approvals/{approval_id}`.",
                                      "awaiting_approval", pending_approval=pending)}
    except ApprovalRequired as exc:
        return {"approval_decision": {"decision": "expired"}, "pending_approval": None,
                "response": _response(f"That proposal can no longer be confirmed ({exc.message}). Ask me to prepare it again.")}
    APPROVALS.labels(pending["action"], "approved" if approve else "rejected").inc()
    return {"approval_decision": {"decision": "approve" if approve else "reject", "decided_by": principal.user_id}}


def route_after_decide(state) -> str:
    decision = (state.get("approval_decision") or {}).get("decision")
    return {"approve": "execute", "reject": "rejected", "expired": "finalize"}.get(decision, "finalize")


async def execute(state) -> dict:
    """Run the approved action via the Ordering Agent, on behalf of the requester."""
    pending = state["pending_approval"]
    update_context(principal=_principal(state))
    task_type = {"create_order": "execute_order", "process_payment": "execute_payment",
                 "cancel_order": "execute_cancel"}[pending["action"]]
    inputs = {"approval_id": pending["approval_id"], "payload": pending["payload"]}
    if pending.get("reuse_key"):
        inputs["idempotency_key"] = pending["reuse_key"]
    task = TaskRequest(task_type=task_type, inputs=inputs)
    result = await OrderingAgent(_cache(state)).handle(task)
    workflow = result.data.get("workflow", "awaiting_retry" if result.status in ("failed", "partial") else "completed")
    update: dict[str, Any] = {
        "task_log": _log_task(state, "ordering_agent", task, result),
        "pending_approval": None, "approval_decision": None, "workflow": workflow,
        "response": _response(result.message, "completed" if result.status == "succeeded" else
                              ("awaiting_retry" if workflow == "awaiting_retry" else "degraded"),
                              data={k: v for k, v in result.data.items() if k in ("order", "steps", "workflow")}),
    }
    if result.data.get("order"):
        update["order"] = result.data["order"]
    if pending["action"] == "create_order" and result.data.get("order"):
        update["order_approval_id"] = pending["approval_id"]
    if workflow == "completed":
        await _cache(state).invalidate("tool", "llm")  # workflow finished → drop derived cache
    AGENT_TURNS.labels(pending["action"], result.status).inc()
    return update


async def rejected(state) -> dict:
    pending = state.get("pending_approval") or {}
    return {"pending_approval": None, "approval_decision": None, "workflow": "idle",
            "response": _response(f"Okay - I've cancelled that proposal ({pending.get('action', 'action')}). "
                                  "Nothing was ordered or charged.")}


async def order_status(state) -> dict:
    update_context(principal=_principal(state))
    entities = state["intent"].get("entities", {})
    task = TaskRequest(task_type="order_status", inputs={k: v for k, v in entities.items() if v},
                       context_refs={"order_id": (state.get("order") or {}).get("order_id")})
    result = await OrderingAgent(_cache(state)).handle(task)
    update: dict[str, Any] = {"task_log": _log_task(state, "ordering_agent", task, result),
                              "response": _response(result.message, "completed" if result.status == "succeeded" else "degraded",
                                                    data=result.data)}
    if result.data.get("order"):
        update["order"] = result.data["order"]
    return update


async def _prepare_ordering_action(state, task_type: str) -> dict:
    entities = state["intent"].get("entities", {})
    task = TaskRequest(task_type=task_type, inputs={k: v for k, v in entities.items() if v},
                       context_refs={"order_id": (state.get("order") or {}).get("order_id"),
                                     "order_approval_id": state.get("order_approval_id"),
                                     "payment": _payment_choice(state, entities.get("payment_token"))})
    result = await OrderingAgent(_cache(state)).handle(task)
    update: dict[str, Any] = {"task_log": _log_task(state, "ordering_agent", task, result)}
    action = result.proposed_action if isinstance(result.proposed_action, dict) else None
    if action is None:
        update["response"] = _response(result.message, "completed" if result.status == "succeeded" else "degraded",
                                       data=result.data)
        return update
    if action.get("reuse_approval"):
        # payment never attempted (service was down) → original approval + key still apply
        approval_id = state["order_approval_id"]
        update["pending_approval"] = {"approval_id": approval_id, "action": "process_payment", "summary": result.message,
                                      "required_role": "buyer", "expires_at": "", "reuse_key": f"pay-{approval_id}",
                                      "payload": action["payload"]}
        update["approval_decision"] = {"decision": "approve", "decided_by": "original-approval"}
        update["workflow"] = "executing"
        update["response"] = {}
        return update
    pending = await _open_approval(state, action["action"], action["payload"], result.message, "buyer")
    APPROVALS.labels(action["action"], "requested").inc()
    update.update({"pending_approval": pending, "workflow": "awaiting_approval",
                   "response": _response(_approval_prompt(pending), "awaiting_approval", pending_approval=pending)})
    return update


async def prepare_retry(state) -> dict:
    return await _prepare_ordering_action(state, "prepare_payment_retry")


async def prepare_cancel(state) -> dict:
    return await _prepare_ordering_action(state, "prepare_cancel")


def route_after_prepare(state) -> str:
    if (state.get("approval_decision") or {}).get("decision") == "approve" and state.get("pending_approval"):
        return "execute"
    return "finalize"


async def respond(state) -> dict:
    intent = state["intent"]["intent"]
    if intent == "greeting":
        return {"response": _response("Hello! " + HELP)}
    text = "I'm not sure how to help with that. " + HELP
    if state.get("pending_approval"):
        text = "You have a pending proposal - reply **yes** to confirm or **no** to cancel.\n\n" + text
    return {"response": _response(text)}


async def finalize(state) -> dict:
    response = dict(state.get("response") or {})
    if not response.get("reply"):
        response = _response("Done.")
    if state.get("pending_approval") and state.get("workflow") == "awaiting_approval":
        response.setdefault("pending_approval", state["pending_approval"])
        if response.get("status") == "completed":
            response["reply"] += "\n\n_(Reminder: a proposal is still waiting for your confirmation.)_"
            response["status"] = "awaiting_approval"
    response["intent"] = (state.get("intent") or {}).get("intent")
    response = check_reply(response)
    messages = [*(state.get("messages") or []), {"role": "assistant", "content": response["reply"][:4000]}]
    AGENT_TURNS.labels(response.get("intent") or "none", response.get("status", "completed")).inc()
    return {"response": response, "messages": messages, "turn": (state.get("turn") or 0) + 1}


def route_after_finalize(state) -> str:
    if state.get("pending_approval") and state.get("workflow") == "awaiting_approval":
        return "approval_gate"
    return "__end__"

