"""Ordering Agent.

Responsibilities: create/read/update orders, coordinate simulated payments and notifications,
and verify final status - only after the supervisor has an approved approval.

Execution is a resumable saga: every step uses an idempotency key derived from the approval ID
(``ord-``/``pay-``/``ntf-``), so re-running after a crash, timeout or partial failure never
duplicates an order, a charge or a notification. Partial failures are reported precisely and
leave the workflow in a retryable state instead of rolling back real-world actions.

Allowed tools (enforced in code): ordering.*, payment.*, notification.*,
recommendation.get_recommendation / get_vendor.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ordering.agents.cache import ThreadCache
from ordering.agents.contracts import NotificationOutcome, PaymentOutcome, TaskRequest, TaskResult
from ordering.agents.mcp_client import get_mcp_client
from ordering.common.context import update_context
from ordering.common.errors import DependencyUnavailable, NotFound, OrderingError
from ordering.common.logging import get_logger
from ordering.security.permissions import ORDERING_AGENT

log = get_logger("ordering.agents.ordering")
UNAVAILABLE_CODES = {"dependency_unavailable", "circuit_open", "dependency_timeout"}
DECLINE_HELP = {
    "card_declined": "the card was declined by the issuer",
    "insufficient_funds": "the account has insufficient funds",
    "expired_card": "the card has expired",
    "limit_exceeded": "the order exceeds the tenant's per-order payment limit",
    "invalid_payment_method": "the payment method is not recognised",
}


def order_brief(o: dict[str, Any]) -> dict[str, Any]:
    return {k: o.get(k) for k in ("order_id", "order_number", "status", "payment_status", "total_amount", "currency",
                                   "location_code", "vendor_code", "expected_delivery_date", "version")}


class OrderingAgent:
    name = ORDERING_AGENT

    def __init__(self, cache: ThreadCache | None = None):
        self.cache = cache
        self.mcp = get_mcp_client()

    async def handle(self, task: TaskRequest) -> TaskResult:
        update_context(agent=self.name)
        handler = {
            "execute_order": self.execute_order,
            "execute_payment": self.execute_payment,
            "execute_cancel": self.execute_cancel,
            "order_status": self.order_status,
            "prepare_payment_retry": self.prepare_payment_retry,
            "prepare_cancel": self.prepare_cancel,
        }[task.task_type]
        try:
            return await handler(task)
        except OrderingError as exc:
            if exc.code in UNAVAILABLE_CODES:
                dependency = str(exc.details.get("dependency", "ordering")).split(":")[-1]
                hint = " It is recovering - try again in about 10 seconds." if exc.code == "circuit_open" else ""
                msg = (f"The {dependency} service is temporarily unavailable. Nothing was changed by this step; "
                       f"it is safe to retry.{hint}")
            else:
                msg = f"I couldn't complete that: {exc.message}."
            return TaskResult(task_id=task.task_id, status="failed", message=msg, errors=[exc.to_dict()])

    # ------------------------------------------------------------------ saga steps
    async def _pay(self, order_id: str, approval_id: str, payment: dict, key: str) -> PaymentOutcome:
        try:
            p = await self.mcp.call("payment.process_payment", {"request": {
                "order_id": order_id, "method": payment["method"], "payment_method_token": payment["payment_method_token"],
                "approval_id": approval_id, "idempotency_key": key}}, cache=self.cache)
        except DependencyUnavailable:
            return PaymentOutcome(status="unavailable")
        return PaymentOutcome(status="captured" if p["status"] == "captured" else "failed", payment_id=p["payment_id"],
                              failure_code=p.get("failure_code"), failure_reason=p.get("failure_reason"),
                              replayed=bool(p.get("replayed")))

    async def _notify(self, order_id: str, template: str, key: str, data: dict, channel: str = "email") -> NotificationOutcome:
        try:
            n = await self.mcp.call("notification.send_notification", {"request": {
                "order_id": order_id, "channel": channel, "template": template, "data": data,
                "idempotency_key": key}}, cache=self.cache)
        except DependencyUnavailable:
            return NotificationOutcome(status="unavailable")
        return NotificationOutcome(status=n["status"], notification_id=n["notification_id"], channel=n["channel"])

    async def _verify(self, order_id: str, wait_for_delivery_s: float = 3.0) -> dict[str, Any]:
        """Read back authoritative state from the system of record (never from memory/LLM)."""
        order = await self.mcp.call("ordering.get_order", {"order_id": order_id})
        payment = None
        try:
            payment = await self.mcp.call("payment.get_payment_status", {"order_id": order_id})
        except DependencyUnavailable:
            pass
        notifications: list[dict] = []
        deadline = asyncio.get_running_loop().time() + wait_for_delivery_s
        while True:
            try:
                notifications = (await self.mcp.call("notification.list_notifications", {"order_id": order_id}))["notifications"]
            except DependencyUnavailable:
                break
            if not notifications or all(n["status"] in ("delivered", "dead_letter") for n in notifications) \
                    or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.5)
        return {"order": order, "payment": payment, "notifications": notifications}

    @staticmethod
    def _status_text(v: dict[str, Any]) -> str:
        o = v["order"]
        lines = [f"**Order {o['order_number']}** - status **{o['status']}**, payment **{o['payment_status']}**, "
                 f"total {o['currency']} {float(o['total_amount']):,.2f}, delivery expected {o['expected_delivery_date']}."]
        for line in o.get("lines", []):
            lines.append(f"- {line['ordered_qty']} × {line['sku']} {line['product_name']} @ ${float(line['unit_cost']):.2f}")
        p = (v.get("payment") or {}).get("latest")
        if p:
            lines.append(f"- Latest payment attempt #{p['attempt']}: {p['status']}"
                         + (f" ({p['failure_code']})" if p.get("failure_code") else f", ref {p['provider_reference']}"))
        for n in v.get("notifications", []):
            lines.append(f"- Notification {n['template']} via {n['channel']}: {n['status']} (attempts {n['attempts']})")
        return "\n".join(lines)

    async def execute_order(self, task: TaskRequest) -> TaskResult:
        """create order → process payment → queue confirmation → verify final status."""
        a = task.inputs
        approval_id, payload = a["approval_id"], a["payload"]
        steps: dict[str, Any] = {}
        order = await self.mcp.call("ordering.create_order", {"request": {
            "recommendation_id": payload["recommendation_id"], "quantity": payload["quantity"],
            "approval_id": approval_id, "idempotency_key": f"ord-{approval_id}"}}, cache=self.cache)
        steps["order"] = {"order_number": order["order_number"], "replayed": bool(order.get("replayed"))}

        payment = await self._pay(order["order_id"], approval_id, payload["payment"], f"pay-{approval_id}")
        steps["payment"] = payment.model_dump(mode="json")
        if payment.status == "captured":
            notification = await self._notify(order["order_id"], "order_confirmation", f"ntf-{approval_id}-confirmation",
                                              {"order_number": order["order_number"], "total": order["total_amount"]})
        elif payment.status == "failed":
            notification = await self._notify(order["order_id"], "payment_failed", f"ntf-{approval_id}-payment-failed",
                                              {"order_number": order["order_number"], "reason": payment.failure_code})
        else:
            notification = NotificationOutcome(status="skipped")
        steps["notification"] = notification.model_dump(mode="json")

        verified = await self._verify(order["order_id"])
        brief = order_brief(verified["order"])
        status_text = self._status_text(verified)
        if payment.status == "captured":
            head = f"✅ Order **{order['order_number']}** was created and paid."
            if notification.status == "unavailable":
                head += " The confirmation notification could not be queued (notification service unavailable) - say \"check the order status\" later."
            return TaskResult(task_id=task.task_id, status="succeeded", message=f"{head}\n\n{status_text}",
                              data={"order": brief, "steps": steps, "workflow": "completed"})
        if payment.status == "failed":
            why = DECLINE_HELP.get(payment.failure_code or "", payment.failure_reason or "the payment failed")
            return TaskResult(task_id=task.task_id, status="partial",
                              message=(f"⚠️ Order **{order['order_number']}** was created, but the simulated payment failed: "
                                       f"{why}. Nothing was charged. Say \"retry payment with tok_mastercard\" (or another "
                                       f"saved method) to try again - I'll ask you to confirm first.\n\n{status_text}"),
                              data={"order": brief, "steps": steps, "workflow": "awaiting_retry"},
                              errors=[{"code": payment.failure_code}])
        return TaskResult(task_id=task.task_id, status="partial",
                          message=(f"⚠️ Order **{order['order_number']}** was created, but the payment service is "
                                   f"unavailable, so no payment was attempted and nothing was charged. Say \"retry payment\" "
                                   f"when it is back - the retry is idempotent and will never double-charge.\n\n{status_text}"),
                          data={"order": brief, "steps": steps, "workflow": "awaiting_retry"},
                          errors=[{"code": "dependency_unavailable", "dependency": "payment"}])

    async def _find_order(self, task: TaskRequest) -> dict[str, Any]:
        e, refs = task.inputs, task.context_refs
        if e.get("order_number"):
            found = (await self.mcp.call("ordering.list_orders", {"filters": {"order_number": e["order_number"], "limit": 1}}))["orders"]
            if not found:
                raise NotFound(f"order {e['order_number']} not found")
            return found[0]
        if refs.get("order_id"):
            return await self.mcp.call("ordering.get_order", {"order_id": refs["order_id"]})
        raise NotFound("which order? Mention the order number (e.g. AR-200001)")

    async def order_status(self, task: TaskRequest) -> TaskResult:
        order = await self._find_order(task)
        verified = await self._verify(order["order_id"], wait_for_delivery_s=0)
        return TaskResult(task_id=task.task_id, status="succeeded", message=self._status_text(verified),
                          data={"order": order_brief(verified["order"])})

    async def prepare_payment_retry(self, task: TaskRequest) -> TaskResult:
        order = await self._find_order(task)
        if order["payment_status"] in ("captured", "authorized"):
            return TaskResult(task_id=task.task_id, status="succeeded",
                              message=f"Order {order['order_number']} is already paid ({order['payment_status']}).",
                              data={"order": order_brief(order)})
        status = await self.mcp.call("payment.get_payment_status", {"order_id": order["order_id"]})
        token = task.inputs.get("payment_token") or task.context_refs.get("payment", {}).get("payment_method_token", "tok_visa")
        payload = {"order_id": order["order_id"], "method": "card", "payment_method_token": token,
                   "order_number": order["order_number"], "amount": order["total_amount"], "currency": order["currency"],
                   "previous_attempts": status["attempts"]}
        # First attempt never happened (payment service was down): the original order approval
        # still covers it and the original idempotency key makes the retry safe.
        reuse = status["attempts"] == 0 and task.context_refs.get("order_approval_id") and not task.inputs.get("payment_token")
        return TaskResult(task_id=task.task_id, status="needs_approval" if not reuse else "succeeded",
                          message=(f"Retry payment of {order['currency']} {float(order['total_amount']):,.2f} for order "
                                   f"{order['order_number']} using {token}."),
                          proposed_action={"action": "process_payment", "payload": payload, "reuse_approval": bool(reuse)},
                          data={"order": order_brief(order)})

    async def execute_payment(self, task: TaskRequest) -> TaskResult:
        a = task.inputs
        payload, approval_id = a["payload"], a["approval_id"]
        key = a.get("idempotency_key") or f"pay-{approval_id}"
        payment = await self._pay(payload["order_id"], approval_id,
                                  {"method": payload["method"], "payment_method_token": payload["payment_method_token"]}, key)
        if payment.status == "unavailable":
            return TaskResult(task_id=task.task_id, status="partial",
                              message="The payment service is still unavailable; nothing was charged. Try again shortly.",
                              data={"workflow": "awaiting_retry"})
        template = "payment_captured" if payment.status == "captured" else "payment_failed"
        await self._notify(payload["order_id"], template, f"ntf-{approval_id}-{template}",
                           {"order_number": payload.get("order_number"), "reason": payment.failure_code})
        verified = await self._verify(payload["order_id"])
        text = self._status_text(verified)
        if payment.status == "captured":
            return TaskResult(task_id=task.task_id, status="succeeded", message=f"✅ Payment captured.\n\n{text}",
                              data={"order": order_brief(verified["order"]), "workflow": "completed"})
        why = DECLINE_HELP.get(payment.failure_code or "", "the payment failed")
        return TaskResult(task_id=task.task_id, status="partial",
                          message=f"⚠️ The payment failed again: {why}. Nothing was charged.\n\n{text}",
                          data={"order": order_brief(verified["order"]), "workflow": "awaiting_retry"})

    async def prepare_cancel(self, task: TaskRequest) -> TaskResult:
        order = await self._find_order(task)
        if order["status"] in ("fulfilled", "cancelled"):
            return TaskResult(task_id=task.task_id, status="failed",
                              message=f"Order {order['order_number']} is {order['status']} and cannot be cancelled.")
        refund = " The captured payment will be refunded (simulated)." if order["payment_status"] == "captured" else ""
        payload = {"order_id": order["order_id"], "order_number": order["order_number"],
                   "reason": task.inputs.get("reason") or "cancelled by buyer via assistant"}
        return TaskResult(task_id=task.task_id, status="needs_approval",
                          message=f"Cancel order {order['order_number']} ({order['currency']} {float(order['total_amount']):,.2f}).{refund}",
                          proposed_action={"action": "cancel_order", "payload": payload}, data={"order": order_brief(order)})

    async def execute_cancel(self, task: TaskRequest) -> TaskResult:
        a = task.inputs
        order = await self.mcp.call("ordering.cancel_order", {"request": {
            "order_id": a["payload"]["order_id"], "reason": a["payload"]["reason"], "approval_id": a["approval_id"],
            "idempotency_key": f"cancel-{a['approval_id']}"}}, cache=self.cache)
        await self._notify(order["order_id"], "order_cancelled", f"ntf-{a['approval_id']}-cancelled",
                           {"order_number": order["order_number"]}, channel="in_app")
        verified = await self._verify(order["order_id"], wait_for_delivery_s=1)
        return TaskResult(task_id=task.task_id, status="succeeded",
                          message=f"Order {order['order_number']} was cancelled.\n\n{self._status_text(verified)}",
                          data={"order": order_brief(verified["order"]), "workflow": "completed"})
