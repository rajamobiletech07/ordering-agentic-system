"""Payment MCP server: simulated payment processing and payment status.

Safety properties:
  * the charged amount is always the order's total from the database;
  * a payment requires approval - either the ``create_order`` approval that included payment
    details (first attempt only) or a dedicated ``process_payment`` approval (e.g. a retry);
  * idempotency key + ``ux_payments_one_success`` partial unique index ⇒ never a double charge;
  * declines are recorded as failed payment attempts (business outcome, not an exception);
    processor outages raise a retryable error and write nothing.
"""

from __future__ import annotations

from uuid import UUID

from ordering.common.context import require_principal
from ordering.common.db import tenant_session
from ordering.common.errors import ApprovalRequired, InvalidState
from ordering.decorators.idempotency import idempotent
from ordering.decorators.observability import audited
from ordering.decorators.tool_access import agent_tool
from ordering.domain.schemas.payment import PaymentStatusResult, ProcessPayment
from ordering.mcp_servers.common import create_mcp, serve
from ordering.mcp_servers.payment import simulator
from ordering.repositories import approvals as approvals_repo
from ordering.repositories import orders as orders_repo
from ordering.repositories import payments as repo

mcp = create_mcp("payment-mcp", "Simulated payment processing and payment status. No real money moves.")


async def _authorize_payment(s, request: ProcessPayment, order) -> None:
    approval = await approvals_repo.get(s, request.approval_id, for_update=True)
    if approval.action == "process_payment":
        await approvals_repo.verify_and_consume(
            s, request.approval_id, "process_payment",
            {"order_id": request.order_id, "method": request.method, "payment_method_token": request.payment_method_token},
        )
        return
    if approval.action == "create_order":
        approved_payment = (approval.payload or {}).get("payment") or {}
        prior = await repo.list_for_order(s, request.order_id)
        if (
            approval.status == "consumed"
            and str(order.order_id) == str(request.order_id)
            and approved_payment.get("method") == request.method
            and approved_payment.get("payment_method_token") == request.payment_method_token
            and not prior
        ):
            return
        raise ApprovalRequired(
            "payment details differ from the approved order, or a retry needs a new payment approval",
            details={"approved_payment": approved_payment, "prior_attempts": len(prior)},
        )
    raise ApprovalRequired("approval does not cover a payment")


@mcp.tool(name="process_payment", annotations={"readOnlyHint": False, "idempotentHint": True, "openWorldHint": False})
@agent_tool("payment", "process_payment")
@audited("payment.process", resource=lambda a, r: str(a["request"].order_id))
@idempotent("payment.process_payment", hash_of=lambda a: a["request"].model_dump(mode="json"))
async def process_payment(request: ProcessPayment) -> dict:
    """Charge the order total using a simulated payment token (see simulator for test tokens)."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        existing = await repo.find_by_idempotency_key(s, request.idempotency_key)
        if existing is not None:
            existing.replayed = True
            return existing.model_dump(mode="json")
        order = await orders_repo.get_order(s, request.order_id, for_update=True)
        if order.payment_status in ("captured", "authorized"):
            raise InvalidState("order is already paid", details={"payment_status": order.payment_status})
        if order.status in ("cancelled", "fulfilled", "draft", "pending_approval"):
            raise InvalidState(f"order in status {order.status} cannot be paid")
        await _authorize_payment(s, request, order)

        limits = await orders_repo.tenant_limits(s, p.tenant_id)
        attempt = len(await repo.list_for_order(s, request.order_id)) + 1
        if order.total_amount > limits["payment_limit"]:
            result = simulator.ChargeResult("failed", None, None, "limit_exceeded",
                                            f"Order total exceeds the tenant payment limit of {limits['payment_limit']}.")
        else:
            result = await simulator.charge(order.total_amount, order.currency, request.method,
                                            request.payment_method_token, request.idempotency_key)
        payment = await repo.insert(
            s, tenant_id=p.tenant_id, order_id=order.order_id, amount=order.total_amount, currency=order.currency,
            method=request.method, last4=result.last4, status=result.status,
            provider_reference=result.provider_reference, failure_code=result.failure_code,
            failure_reason=result.failure_reason, attempt=attempt, idempotency_key=request.idempotency_key,
            approval_id=request.approval_id,
        )
        if result.status == "captured":
            await orders_repo.update_status(s, order.order_id, status="submitted", payment_status="captured")
        else:
            await orders_repo.update_status(s, order.order_id, status="payment_failed", payment_status="failed")
    return payment.model_dump(mode="json")


@mcp.tool(name="get_payment_status", annotations={"readOnlyHint": True, "idempotentHint": True})
@agent_tool("payment", "get_payment_status")
async def get_payment_status(order_id: UUID) -> dict:
    """Latest payment attempt and the order's authoritative payment status."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        order = await orders_repo.get_order(s, order_id)
        attempts = await repo.list_for_order(s, order_id)
    return PaymentStatusResult(
        order_id=order_id, order_payment_status=order.payment_status,
        latest=attempts[0] if attempts else None, attempts=len(attempts),
    ).model_dump(mode="json")


def main() -> None:
    serve(mcp, "mcp-payment", 9003)


if __name__ == "__main__":
    main()
