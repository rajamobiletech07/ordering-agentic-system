"""Recommendation Agent.

Responsibilities: retrieve and explain replenishment recommendations, investigate inventory risk
(bounded read-only tool loop), answer policy questions (RAG), and turn a chosen recommendation
into an :class:`OrderProposal` handed back to the supervisor.

Allowed tools (enforced in code): recommendation.* (read) and ordering.list_orders (read).
Transactional facts always come from MCP tools; RAG is used only for policy/guideline text.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from ordering.agents.cache import ThreadCache
from ordering.agents.contracts import OrderProposal, PaymentSelection, TaskRequest, TaskResult, money
from ordering.agents.guardrails import wrap_untrusted
from ordering.agents.mcp_client import get_mcp_client
from ordering.common.context import update_context
from ordering.common.errors import InvalidState, NotFound, OrderingError
from ordering.common.logging import get_logger
from ordering.governance.policy import get_policy
from ordering.llm_client.client import get_llm_client
from ordering.prompts import registry
from ordering.rag.answerer import answer_question
from ordering.security.permissions import RECOMMENDATION_AGENT

log = get_logger("ordering.agents.recommendation")
RISK_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}


def _fmt_rec_line(i: int, r: dict[str, Any]) -> str:
    return (f"{i}. {RISK_ICON.get(r['risk_level'], '')} **{r['sku']}** {r['product_name']} @ {r['location_code']} - "
            f"order **{r['recommended_qty']}** from {r['vendor_name']} (${float(r['unit_cost']):.2f}/unit, "
            f"≈ ${float(r['estimated_total']):,.2f}); stock {r['current_stock']} vs safety {r['safety_stock']}, "
            f"{r['risk_level']} risk, ETA {r['expected_delivery_date']}")


def compact(r: dict[str, Any]) -> dict[str, Any]:
    keys = ("recommendation_id", "sku", "product_name", "location_code", "vendor_code", "vendor_name", "recommended_qty",
            "unit_cost", "estimated_total", "risk_level", "current_stock", "safety_stock", "expected_delivery_date",
            "status")
    return {k: r.get(k) for k in keys}


class InvestigationStep(BaseModel):
    action: Literal["list_inventory_risks", "get_inventory_position", "list_orders", "finish"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class RecommendationAgent:
    name = RECOMMENDATION_AGENT

    def __init__(self, cache: ThreadCache | None = None, sticky_key: str | None = None):
        self.cache, self.sticky_key = cache, sticky_key
        self.mcp = get_mcp_client()

    async def handle(self, task: TaskRequest) -> TaskResult:
        update_context(agent=self.name)
        handler = {
            "get_recommendations": self.get_recommendations,
            "explain_recommendation": self.explain_recommendation,
            "investigate_risk": self.investigate_risk,
            "policy_question": self.policy_question,
            "build_proposal": self.build_proposal,
        }[task.task_type]
        try:
            return await handler(task)
        except OrderingError as exc:
            # not_found here means "which item?" - a clarification question, not a failure
            status = "succeeded" if exc.code in ("not_found", "invalid_state") else ("degraded" if exc.retryable else "failed")
            return TaskResult(task_id=task.task_id, status=status, message=self._error_message(exc),
                              errors=[exc.to_dict()])

    @staticmethod
    def _error_message(exc: OrderingError) -> str:
        if exc.code in ("dependency_unavailable", "circuit_open", "dependency_timeout"):
            return ("The recommendation service is temporarily unavailable, so I can't read live recommendation data "
                    "right now. Nothing has been changed - please try again in a moment.")
        if exc.code == "not_found":
            return f"I couldn't find that in your data: {exc.message}."
        return f"I couldn't complete that: {exc.message}."

    # ------------------------------------------------------------------ tasks
    async def get_recommendations(self, task: TaskRequest) -> TaskResult:
        e = task.inputs
        filters: dict[str, Any] = {"status": "open", "limit": int(e.get("limit") or 5)}
        for key in ("sku", "location_code", "category"):
            if e.get(key):
                filters[key] = e[key]
        if e.get("risk_level"):
            filters["risk_levels"] = [e["risk_level"]]
        data = await self.mcp.call("recommendation.list_recommendations", {"filters": filters}, cache=self.cache)
        recs = data.get("recommendations", [])
        if not recs:
            return TaskResult(task_id=task.task_id, status="succeeded",
                              message="There are no open replenishment recommendations matching that request.",
                              data={"recommendations": []})
        scope = " at " + filters["location_code"] if filters.get("location_code") else ""
        lines = [_fmt_rec_line(i, r) for i, r in enumerate(recs, start=1)]
        message = (f"Here are the top {len(recs)} open replenishment recommendations{scope}, highest risk first:\n"
                   + "\n".join(lines)
                   + "\n\nAsk me *why* for any item, or say e.g. \"order the first one\" to prepare an order.")
        return TaskResult(task_id=task.task_id, status="succeeded", message=message,
                          data={"recommendations": [compact(r) for r in recs]})

    async def _resolve_recommendation(self, task: TaskRequest) -> dict[str, Any]:
        e, refs = task.inputs, task.context_refs
        listed: list[dict] = refs.get("last_recommendations") or []
        if e.get("recommendation_index") and listed:
            idx = int(e["recommendation_index"]) - 1
            if 0 <= idx < len(listed):
                return await self.mcp.call("recommendation.get_recommendation",
                                           {"recommendation_id": listed[idx]["recommendation_id"]}, cache=self.cache)
        if e.get("sku"):
            filters: dict[str, Any] = {"status": "open", "sku": e["sku"], "limit": 5}
            if e.get("location_code"):
                filters["location_code"] = e["location_code"]
            recs = (await self.mcp.call("recommendation.list_recommendations", {"filters": filters},
                                        cache=self.cache)).get("recommendations", [])
            if not recs:
                await self._explain_missing(e)
            return recs[0]
        focus = refs.get("focus_recommendation_id") or (listed[0]["recommendation_id"] if len(listed) == 1 else None)
        if focus:
            return await self.mcp.call("recommendation.get_recommendation", {"recommendation_id": focus}, cache=self.cache)
        raise NotFound("which item do you mean? Mention a SKU (e.g. SKU-10042) or its number in the list")

    async def _explain_missing(self, e: dict[str, Any]) -> None:
        """No open recommendation: say *why* (e.g. already ordered - with the order number)."""
        where = f" at {e['location_code']}" if e.get("location_code") else ""
        filters: dict[str, Any] = {"status": None, "sku": e["sku"], "limit": 5}
        if e.get("location_code"):
            filters["location_code"] = e["location_code"]
        others = (await self.mcp.call("recommendation.list_recommendations", {"filters": filters})).get("recommendations", [])
        ordered = next((r for r in others if r["status"] == "ordered"), None)
        if ordered:
            orders = (await self.mcp.call("ordering.list_orders", {"filters": {
                "recommendation_id": ordered["recommendation_id"], "limit": 1}})).get("orders", [])
            ref = f" as order {orders[0]['order_number']} ({orders[0]['status']}, payment {orders[0]['payment_status']})" if orders else ""
            raise InvalidState(f"{e['sku']}{where} was already ordered{ref}, so I won't create a duplicate")
        raise NotFound(f"there is no open recommendation for {e['sku']}{where}")

    async def explain_recommendation(self, task: TaskRequest) -> TaskResult:
        rec = await self._resolve_recommendation(task)
        position = await self.mcp.call("recommendation.get_inventory_position",
                                       {"sku": rec["sku"], "location_code": rec["location_code"]}, cache=self.cache)
        vendor = await self.mcp.call("recommendation.get_vendor", {"vendor_code": rec["vendor_code"]}, cache=self.cache)
        guideline = await answer_question("How are replenishment recommendations and risk levels calculated?",
                                          task.context_refs["tenant_id"], cache=self.cache, sticky_key=self.sticky_key)
        records = {"recommendation": rec, "inventory_position": position, "vendor": vendor}
        fallback = self._explain_template(rec, position, vendor, guideline.citations[:1])
        prompt = await registry.resolve("recommendation_explainer", self.sticky_key)
        system, user = prompt.render(question=task.inputs.get("question", "Why is this recommended?"),
                                     records=json.dumps(records, default=str, indent=1))
        result = await get_llm_client().complete(prompt.route, system=system, user=user, fallback=fallback,
                                                 prompt_id=prompt.prompt_id, prompt_version=prompt.version,
                                                 cache=self.cache)
        return TaskResult(task_id=task.task_id, status="degraded" if result.degraded else "succeeded",
                          message=result.text, citations=guideline.citations[:1],
                          data={"focus_recommendation": compact(rec), "prompt": prompt.ref,
                                "llm": {"provider": result.provider, "model": result.model}})

    @staticmethod
    def _explain_template(rec, pos, vendor, citations) -> str:
        cover = float(pos["days_of_cover"])
        lead = vendor["lead_time_days"]
        risk_note = ("a stock-out is expected before the next delivery" if cover < lead
                     else "stock covers the lead time but will fall below safety stock")
        cite = f" [{citations[0].ref}]" if citations else ""
        return (
            f"**Why {rec['sku']} ({rec['product_name']}) at {rec['location_code']} is recommended**\n"
            f"- On hand: {pos['on_hand']} units, safety stock {pos['safety_stock']}, average demand "
            f"{pos['avg_daily_demand']}/day → {pos['days_of_cover']} days of cover vs a {lead}-day vendor lead time, so "
            f"{risk_note} ({rec['risk_level']} risk).\n"
            f"- Forecast: {rec['forecast_demand']} units over {rec['forecast_horizon_days']} days.\n"
            f"- Quantity: forecast + safety stock - on hand - on order, rounded up to {vendor['name']}'s case pack of "
            f"{vendor['min_order_qty']} → **{rec['recommended_qty']} units**{cite}.\n"
            f"- Cost: ${float(rec['unit_cost']):.2f}/unit ≈ **${float(rec['estimated_total']):,.2f}**; expected delivery "
            f"{rec['expected_delivery_date']}.\n"
            f"- Engine note: {rec['reason']}\n\n"
            f"Next step: say \"order it\" to prepare a purchase order for your confirmation."
        )

    async def investigate_risk(self, task: TaskRequest) -> TaskResult:
        """Bounded read-only tool loop: the planner LLM picks the next allowlisted tool; a
        deterministic plan is the fallback. Max steps come from governance policy."""
        max_steps = get_policy().autonomy.max_investigation_steps
        e = task.inputs
        steps: list[dict[str, Any]] = []
        prompt = await registry.resolve("risk_investigator", self.sticky_key)
        observations: dict[str, Any] = {}
        for step_no in range(max_steps):
            planned = self._next_step_plan(step_no, e, observations)
            system, user = prompt.render(tools="list_inventory_risks, get_inventory_position, list_orders, finish",
                                         question=e.get("question", ""),
                                         steps=wrap_untrusted("tool_results", json.dumps(steps, default=str)[:6000]))
            decision = await get_llm_client().complete(prompt.route, system=system, user=user, schema=InvestigationStep,
                                                       fallback=planned, prompt_id=prompt.prompt_id,
                                                       prompt_version=prompt.version)
            step: InvestigationStep = decision.parsed
            if step.action == "finish":
                break
            tool = {"list_inventory_risks": "recommendation.list_inventory_risks",
                    "get_inventory_position": "recommendation.get_inventory_position",
                    "list_orders": "ordering.list_orders"}[step.action]
            args = step.arguments if step.action != "list_orders" else {"filters": step.arguments.get("filters", step.arguments)}
            try:
                result = await self.mcp.call(tool, args, cache=self.cache)
            except OrderingError as exc:
                result = {"error": exc.code, "message": exc.message}
            steps.append({"action": step.action, "arguments": args, "result": result})
            observations[step.action] = result
        return TaskResult(task_id=task.task_id, status="succeeded", message=self._risk_summary(observations, e),
                          data={"investigation_steps": [{"action": s["action"], "arguments": s["arguments"]} for s in steps],
                                "risks": (observations.get("list_inventory_risks") or {}).get("risks", [])[:5]})

    @staticmethod
    def _next_step_plan(step_no: int, e: dict, obs: dict) -> InvestigationStep:
        if "list_inventory_risks" not in obs:
            args = {k: e[k] for k in ("location_code", "category") if e.get(k)}
            args["limit"] = 5
            return InvestigationStep(action="list_inventory_risks", arguments=args, reason="find items below safety stock")
        risks = obs["list_inventory_risks"].get("risks", []) if isinstance(obs["list_inventory_risks"], dict) else []
        if risks and "get_inventory_position" not in obs:
            worst = risks[0]
            return InvestigationStep(action="get_inventory_position",
                                     arguments={"sku": worst["sku"], "location_code": worst["location_code"]},
                                     reason="inspect the most severe item")
        if risks and "list_orders" not in obs:
            return InvestigationStep(action="list_orders", arguments={"filters": {"sku": risks[0]["sku"], "limit": 5}},
                                     reason="check whether an order is already in flight")
        return InvestigationStep(action="finish", reason="enough evidence")

    @staticmethod
    def _risk_summary(obs: dict, e: dict) -> str:
        risks = (obs.get("list_inventory_risks") or {}).get("risks", [])
        if not risks:
            return "No items are currently below safety stock" + (f" at {e['location_code']}." if e.get("location_code") else ".")
        lines = [f"{i}. {RISK_ICON.get(r['risk_level'], '')} **{r['sku']}** {r['product_name']} @ {r['location_code']}: "
                 f"{r['on_hand']} on hand vs safety {r['safety_stock']} ({r['days_of_cover']} days of cover)"
                 + (" - open recommendation available" if r.get("open_recommendation_id") else " - no open recommendation")
                 for i, r in enumerate(risks, start=1)]
        text = "**Inventory risk investigation**\n" + "\n".join(lines)
        orders = (obs.get("list_orders") or {}).get("orders")
        if orders is not None:
            open_orders = [o for o in orders if o["status"] in ("approved", "submitted", "pending_approval")]
            text += (f"\n\nFor {risks[0]['sku']}: {len(open_orders)} order(s) already in flight"
                     + (f" (latest {open_orders[0]['order_number']}, {open_orders[0]['status']})." if open_orders else
                        " - none, so the stock-out risk is not yet covered."))
        return text + "\n\nAsk for recommendations to see suggested order quantities."

    async def policy_question(self, task: TaskRequest) -> TaskResult:
        result = await answer_question(task.inputs["question"], task.context_refs["tenant_id"], cache=self.cache,
                                       sticky_key=self.sticky_key)
        status = "insufficient_evidence" if not result.sufficient else ("degraded" if result.degraded else "succeeded")
        return TaskResult(task_id=task.task_id, status=status, message=result.answer, citations=result.citations,
                          data={"prompt": result.prompt_ref, "stripped_untrusted_lines": len(result.stripped_lines)})

    async def build_proposal(self, task: TaskRequest) -> TaskResult:
        """Handoff contract: turn the chosen recommendation into an OrderProposal."""
        rec = await self._resolve_recommendation(task)
        if rec["status"] not in ("open", "accepted"):
            raise InvalidState(f"recommendation for {rec['sku']} is already {rec['status']}")
        quantity = int(task.inputs.get("quantity") or rec["recommended_qty"])
        vendor = await self.mcp.call("recommendation.get_vendor", {"vendor_code": rec["vendor_code"]}, cache=self.cache)
        moq = int(vendor["min_order_qty"])
        adjusted_note = ""
        if quantity % moq:
            rounded = ((quantity // moq) + 1) * moq
            adjusted_note = f" (rounded up from {quantity} to the case pack of {moq})"
            quantity = rounded
        unit_cost = money(rec["unit_cost"])
        total = money(unit_cost * quantity)
        threshold = money(task.context_refs.get("approval_threshold", "10000"))
        payment = PaymentSelection(**task.context_refs["payment"]) if task.context_refs.get("payment") else PaymentSelection()
        proposal = OrderProposal(
            recommendation_id=rec["recommendation_id"], sku=rec["sku"], product_name=rec["product_name"],
            location_code=rec["location_code"], vendor_code=rec["vendor_code"], vendor_name=rec["vendor_name"],
            quantity=quantity, recommended_qty=rec["recommended_qty"], unit_cost=unit_cost, total=total,
            expected_delivery_date=rec["expected_delivery_date"], risk_level=rec["risk_level"],
            rationale=rec["reason"] + adjusted_note, requires_role="approver" if total > threshold else "buyer",
            payment=payment,
        )
        return TaskResult(task_id=task.task_id, status="needs_approval", message=proposal.summary(),
                          proposed_action=proposal, data={"focus_recommendation": compact(rec)})
