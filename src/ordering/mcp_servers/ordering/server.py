"""Ordering MCP server: create, read and update orders.

Every mutation:
  * requires a human approval bound to the exact parameters (verified in the same transaction),
  * is idempotent (``idempotency_key``; DB-backed replay + unique constraint),
  * reads price/vendor/location from the database - never from the caller,
  * uses optimistic locking (``version``) for updates, and is audited.
"""

from __future__ import annotations

from uuid import UUID

from ordering.common.context import require_principal
from ordering.common.db import tenant_session
from ordering.common.errors import ApprovalRequired, InvalidState
from ordering.decorators.idempotency import idempotent
from ordering.decorators.observability import audited
from ordering.decorators.tool_access import agent_tool
from ordering.domain.schemas.order import CancelOrder, CreateOrderFromRecommendation, OrderFilter, UpdateOrder
from ordering.mcp_servers.common import create_mcp, serve
from ordering.repositories import approvals as approvals_repo
from ordering.repositories import orders as repo
from ordering.repositories import recommendations as rec_repo

mcp = create_mcp("ordering-mcp", "Create, read, update and cancel purchase orders. Mutations need human approval.")
READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
MUTATION = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

EDITABLE_STATUSES = {"draft", "pending_approval", "approved", "payment_failed"}


def _order_resource(args, result):
    return str(result.get("order_id")) if isinstance(result, dict) else None


@mcp.tool(name="list_orders", annotations=READ_ONLY)
@agent_tool("ordering", "list_orders")
async def list_orders(filters: OrderFilter) -> dict:
    """List orders (newest first) by status, payment status, SKU, location or recommendation."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        orders = await repo.list_orders(s, filters)
    return {"orders": [o.model_dump(mode="json") for o in orders], "count": len(orders)}


@mcp.tool(name="get_order", annotations=READ_ONLY)
@agent_tool("ordering", "get_order")
async def get_order(order_id: UUID) -> dict:
    """Get an order with its lines, order status and payment status (authoritative)."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        order = await repo.get_order(s, order_id)
    return order.model_dump(mode="json")


@mcp.tool(name="create_order", annotations=MUTATION)
@agent_tool("ordering", "create_order")
@audited("order.create", resource=_order_resource)
@idempotent("ordering.create_order", hash_of=lambda a: a["request"].model_dump(mode="json", exclude={"notes"}))
async def create_order(request: CreateOrderFromRecommendation) -> dict:
    """Create a purchase order from a recommendation after the user approved it.

    Requires ``approval_id`` for a ``create_order`` approval whose recommendation_id and quantity
    match exactly. Unit cost, vendor and location are taken from the recommendation record.
    """
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        existing = await repo.find_by_idempotency_key(s, request.idempotency_key)
        if existing is not None:
            existing.replayed = True
            return existing.model_dump(mode="json")

        rec = await rec_repo.get_recommendation(s, request.recommendation_id, for_update=True)
        if rec.status not in ("open", "accepted"):
            raise InvalidState(
                f"recommendation is already {rec.status}",
                details={"recommendation_id": str(rec.recommendation_id), "status": rec.status},
            )
        approval = await approvals_repo.verify_and_consume(
            s, request.approval_id, "create_order",
            {"recommendation_id": request.recommendation_id, "quantity": request.quantity},
        )
        limits = await repo.tenant_limits(s, p.tenant_id)
        total = rec.unit_cost * request.quantity
        if total > limits["approval_threshold"] and approval.required_role not in ("approver", "admin"):
            raise ApprovalRequired("order total exceeds the approval threshold; an approver must approve",
                                   details={"threshold": str(limits["approval_threshold"])})
        ids = await repo.product_for_recommendation(s, rec.recommendation_id)
        order_id = await repo.insert_order(
            s,
            tenant_id=p.tenant_id,
            recommendation_id=rec.recommendation_id,
            location_id=ids["location_id"],
            vendor_id=ids["vendor_id"],
            product_id=ids["product_id"],
            sku=rec.sku,
            quantity=request.quantity,
            unit_cost=rec.unit_cost,
            currency=limits["currency"],
            expected_delivery_date=rec.expected_delivery_date,
            status="approved",
            idempotency_key=request.idempotency_key,
            approval_id=request.approval_id,
            created_by=approval.requested_by,
            notes=request.notes,
        )
        await rec_repo.set_status(s, rec.recommendation_id, "ordered")
        order = await repo.get_order(s, order_id)
    return order.model_dump(mode="json")


@mcp.tool(name="update_order", annotations=MUTATION)
@agent_tool("ordering", "update_order")
@audited("order.update", resource=lambda a, r: str(a["request"].order_id))
@idempotent("ordering.update_order", hash_of=lambda a: a["request"].model_dump(mode="json"))
async def update_order(request: UpdateOrder) -> dict:
    """Change quantity and/or expected delivery date of an unpaid order (approval + optimistic lock)."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        order = await repo.get_order(s, request.order_id, for_update=True)
        if order.status not in EDITABLE_STATUSES or order.payment_status in ("authorized", "captured", "pending"):
            raise InvalidState(f"order in status {order.status}/{order.payment_status} cannot be modified")
        await approvals_repo.verify_and_consume(
            s, request.approval_id, "update_order",
            {"order_id": request.order_id, "quantity": request.quantity,
             "expected_delivery_date": request.expected_delivery_date},
        )
        version = request.expected_version
        if request.quantity is not None:
            await repo.update_quantity(s, request.order_id, request.quantity, version)
            version += 1
        if request.expected_delivery_date is not None:
            await repo.update_delivery_date(s, request.order_id, request.expected_delivery_date)
            if request.quantity is None:
                await repo.update_status(s, request.order_id, expected_version=version)
        order = await repo.get_order(s, request.order_id)
    return order.model_dump(mode="json")


@mcp.tool(name="cancel_order", annotations={**MUTATION, "destructiveHint": True})
@agent_tool("ordering", "cancel_order")
@audited("order.cancel", resource=lambda a, r: str(a["request"].order_id))
@idempotent("ordering.cancel_order", hash_of=lambda a: a["request"].model_dump(mode="json"))
async def cancel_order(request: CancelOrder) -> dict:
    """Cancel an order that is not yet fulfilled. A captured payment is (simulated) refunded."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        order = await repo.get_order(s, request.order_id, for_update=True)
        if order.status in ("fulfilled", "cancelled"):
            raise InvalidState(f"order is already {order.status}")
        await approvals_repo.verify_and_consume(s, request.approval_id, "cancel_order", {"order_id": request.order_id})
        payment_status = "refunded" if order.payment_status in ("captured", "authorized") else None
        await repo.update_status(s, request.order_id, status="cancelled", payment_status=payment_status,
                                 expected_version=order.version)
        if order.recommendation_id:
            # the need still exists; "accepted" lets the user re-order without violating the
            # one-open-recommendation-per-item rule
            await rec_repo.set_status(s, order.recommendation_id, "accepted")
        order = await repo.get_order(s, request.order_id)
    return order.model_dump(mode="json")


def main() -> None:
    serve(mcp, "mcp-ordering", 9002)


if __name__ == "__main__":
    main()
