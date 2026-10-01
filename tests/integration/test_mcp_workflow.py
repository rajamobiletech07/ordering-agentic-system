"""MCP tools against real Postgres: approvals, idempotency, payments, notifications, isolation."""

import uuid

import pytest
from sqlalchemy import text

from ordering.common.db import admin_session, tenant_session
from ordering.common.errors import (
    ApprovalRequired,
    IdempotencyConflict,
    InvalidState,
    NotFound,
    ToolAccessDenied,
)
from ordering.notification_worker.worker import run_once
from ordering.repositories import approvals

pytestmark = pytest.mark.integration


async def _open_rec(max_total=2000) -> dict:
    async with admin_session() as s:
        row = (await s.execute(text(
            """SELECT recommendation_id, recommended_qty FROM recommendations
               WHERE tenant_id = 'acme-retail' AND status = 'open' AND unit_cost * recommended_qty < :m
               ORDER BY random() LIMIT 1"""), {"m": max_total})).one()
    return {"recommendation_id": str(row.recommendation_id), "quantity": row.recommended_qty}


async def _approve(principal, action, payload, approver=None):
    async with tenant_session(principal.tenant_id, principal.user_id) as s:
        a = await approvals.create(s, principal=principal, conversation_id=None, action=action, payload=payload,
                                   summary="test", required_role="buyer", ttl_minutes=5)
        await approvals.decide(s, a.approval_id, approver or principal, True, None)
    return str(a.approval_id)


async def test_order_lifecycle_idempotency_and_no_double_charge(mcp, as_user):
    buyer = as_user()
    rec = await _open_rec()
    payload = {**rec, "payment": {"method": "card", "payment_method_token": "tok_decline"}}
    approval_id = await _approve(buyer, "create_order", payload)
    key = f"it-{uuid.uuid4()}"
    request = {"request": {**rec, "approval_id": approval_id, "idempotency_key": key}}

    order = await mcp.call("ordering.create_order", request)
    assert order["status"] == "approved" and order["payment_status"] == "not_started"
    replay = await mcp.call("ordering.create_order", request)                 # duplicate request
    assert replay["order_id"] == order["order_id"] and replay["replayed"] is True
    with pytest.raises(IdempotencyConflict):                                  # same key, different body
        await mcp.call("ordering.create_order", {"request": {**request["request"], "quantity": rec["quantity"] + 12}})

    # first payment (covered by the order approval) is declined → recorded, order marked payment_failed
    pay = {"order_id": order["order_id"], "method": "card", "payment_method_token": "tok_decline",
           "approval_id": approval_id, "idempotency_key": f"pay-{key}"}
    declined = await mcp.call("payment.process_payment", {"request": pay})
    assert declined["status"] == "failed" and declined["failure_code"] == "card_declined"
    assert (await mcp.call("ordering.get_order", {"order_id": order["order_id"]}))["status"] == "payment_failed"
    # a retry needs a NEW approval
    with pytest.raises(ApprovalRequired):
        await mcp.call("payment.process_payment", {"request": {**pay, "payment_method_token": "tok_visa",
                                                               "idempotency_key": f"pay2-{key}"}})
    retry_fields = {"order_id": order["order_id"], "method": "card", "payment_method_token": "tok_visa"}
    retry_approval = await _approve(buyer, "process_payment", retry_fields)
    captured = await mcp.call("payment.process_payment", {"request": {**retry_fields, "approval_id": retry_approval,
                                                                      "idempotency_key": f"pay3-{key}"}})
    assert captured["status"] == "captured" and captured["attempt"] == 2
    # never a second successful charge, even with a fresh approval
    again = await _approve(buyer, "process_payment", retry_fields)
    with pytest.raises(InvalidState):
        await mcp.call("payment.process_payment", {"request": {**retry_fields, "approval_id": again,
                                                               "idempotency_key": f"pay4-{key}"}})
    status = await mcp.call("payment.get_payment_status", {"order_id": order["order_id"]})
    assert status["order_payment_status"] == "captured" and status["attempts"] == 2

    # notification: enqueue (idempotent) → async worker delivers → status tracked
    n_req = {"request": {"order_id": order["order_id"], "template": "order_confirmation", "channel": "email",
                         "idempotency_key": f"ntf-{key}"}}
    queued = await mcp.call("notification.send_notification", n_req)
    assert queued["status"] == "queued"
    assert (await mcp.call("notification.send_notification", n_req))["notification_id"] == queued["notification_id"]
    for _ in range(5):
        await run_once()
    delivered = await mcp.call("notification.get_notification_status", {"notification_id": queued["notification_id"]})
    assert delivered["status"] == "delivered" and delivered["attempts"] == 1


async def test_approval_binding_blocks_parameter_changes(mcp, as_user):
    buyer = as_user()
    rec = await _open_rec()
    approval_id = await _approve(buyer, "create_order", {**rec, "payment": {"method": "card", "payment_method_token": "tok_visa"}})
    with pytest.raises(ApprovalRequired):   # e.g. prompt-injected quantity change after approval
        await mcp.call("ordering.create_order", {"request": {**rec, "quantity": rec["quantity"] * 10,
                                                             "approval_id": approval_id,
                                                             "idempotency_key": f"it-{uuid.uuid4()}"}})
    unapproved = str(uuid.uuid4())
    with pytest.raises(NotFound):
        await mcp.call("ordering.create_order", {"request": {**rec, "approval_id": unapproved,
                                                             "idempotency_key": f"it-{uuid.uuid4()}"}})


async def test_tool_allowlist_and_tenant_isolation(mcp, as_user):
    as_user(agent="recommendation_agent")
    with pytest.raises(ToolAccessDenied):
        await mcp.call("ordering.create_order", {"request": {}})
    risks = await mcp.call("recommendation.list_inventory_risks", {"limit": 3})
    assert risks["count"] >= 1

    as_user(agent="ordering_agent")
    async with admin_session() as s:
        acme_order = (await s.execute(text("SELECT order_id FROM orders WHERE tenant_id='acme-retail' LIMIT 1"))).scalar_one()
    as_user("u-globex-buyer", "globex-grocers", ("buyer",), "ordering_agent")
    with pytest.raises(NotFound):          # RLS: another tenant's order is invisible
        await mcp.call("ordering.get_order", {"order_id": str(acme_order)})


async def test_viewer_cannot_mutate(mcp, as_user):
    as_user("u-acme-viewer", roles=("viewer",))
    with pytest.raises(ToolAccessDenied):
        await mcp.call("payment.process_payment", {"request": {}})
