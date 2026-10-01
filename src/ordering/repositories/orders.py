"""Order persistence. All writes happen inside the caller's tenant-scoped transaction."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ordering.common.errors import Conflict, NotFound
from ordering.domain.schemas.order import Order, OrderFilter, OrderLine

_ORDER_SELECT = """
SELECT o.order_id, o.order_number, o.recommendation_id, l.location_code, v.vendor_code, v.name AS vendor_name,
       o.status, o.payment_status, o.currency, o.total_amount, o.expected_delivery_date, o.created_by, o.version,
       o.created_at, o.updated_at
FROM orders o
JOIN locations l ON l.tenant_id = o.tenant_id AND l.location_id = o.location_id
JOIN vendors v ON v.tenant_id = o.tenant_id AND v.vendor_id = o.vendor_id
"""


async def _lines(session: AsyncSession, order_ids: list[UUID]) -> dict[UUID, list[OrderLine]]:
    if not order_ids:
        return {}
    rows = await session.execute(
        text(
            """SELECT ol.order_id, ol.line_no, ol.sku, p.name AS product_name, ol.ordered_qty, ol.unit_cost, ol.line_total
               FROM order_lines ol JOIN products p ON p.tenant_id = ol.tenant_id AND p.product_id = ol.product_id
               WHERE ol.order_id = ANY(:ids) ORDER BY ol.order_id, ol.line_no"""
        ),
        {"ids": order_ids},
    )
    out: dict[UUID, list[OrderLine]] = {}
    for r in rows:
        m = dict(r._mapping)
        out.setdefault(m.pop("order_id"), []).append(OrderLine.model_validate(m))
    return out


async def get_order(session: AsyncSession, order_id: UUID, for_update: bool = False) -> Order:
    row = (
        await session.execute(
            text(_ORDER_SELECT + " WHERE o.order_id = :id" + (" FOR UPDATE OF o" if for_update else "")),
            {"id": order_id},
        )
    ).first()
    if row is None:
        raise NotFound("order not found", details={"order_id": str(order_id)})
    order = Order.model_validate(dict(row._mapping))
    order.lines = (await _lines(session, [order.order_id])).get(order.order_id, [])
    return order


async def find_by_idempotency_key(session: AsyncSession, key: str) -> Order | None:
    row = (await session.execute(text("SELECT order_id FROM orders WHERE idempotency_key = :k"), {"k": key})).first()
    return await get_order(session, row.order_id) if row else None


async def list_orders(session: AsyncSession, f: OrderFilter) -> list[Order]:
    rows = await session.execute(
        text(
            _ORDER_SELECT
            + """
            WHERE (CAST(:status AS TEXT) IS NULL OR o.status = :status)
              AND (CAST(:pstatus AS TEXT) IS NULL OR o.payment_status = :pstatus)
              AND (CAST(:loc AS TEXT) IS NULL OR l.location_code = :loc)
              AND (CAST(:rec AS UUID) IS NULL OR o.recommendation_id = :rec)
              AND (CAST(:num AS TEXT) IS NULL OR o.order_number = :num)
              AND (CAST(:sku AS TEXT) IS NULL OR EXISTS (
                    SELECT 1 FROM order_lines ol WHERE ol.tenant_id = o.tenant_id AND ol.order_id = o.order_id
                      AND ol.sku = :sku))
            ORDER BY o.created_at DESC LIMIT :limit"""
        ),
        {"status": f.status, "pstatus": f.payment_status, "loc": f.location_code, "rec": f.recommendation_id,
         "sku": f.sku, "num": f.order_number, "limit": f.limit},
    )
    orders = [Order.model_validate(dict(r._mapping)) for r in rows]
    lines = await _lines(session, [o.order_id for o in orders])
    for o in orders:
        o.lines = lines.get(o.order_id, [])
    return orders


async def insert_order(
    session: AsyncSession,
    *,
    tenant_id: str,
    recommendation_id: UUID | None,
    location_id: UUID,
    vendor_id: UUID,
    product_id: UUID,
    sku: str,
    quantity: int,
    unit_cost: Decimal,
    currency: str,
    expected_delivery_date: date,
    status: str,
    idempotency_key: str,
    approval_id: UUID | None,
    created_by: str,
    notes: str | None,
) -> UUID:
    order_id = uuid4()
    seq = (await session.execute(text("SELECT nextval('order_number_seq')"))).scalar_one()
    prefix = "".join(part[0] for part in tenant_id.split("-")).upper()[:3]
    total = (unit_cost * quantity).quantize(Decimal("0.01"))
    await session.execute(
        text(
            """INSERT INTO orders (order_id, tenant_id, order_number, recommendation_id, location_id, vendor_id, status,
                   payment_status, currency, total_amount, expected_delivery_date, idempotency_key, approval_id,
                   created_by, notes)
               VALUES (:id, :t, :num, :rec, :loc, :v, :st, 'not_started', :cur, :total, :edd, :key, :appr, :by, :notes)"""
        ),
        {"id": order_id, "t": tenant_id, "num": f"{prefix}-{seq}", "rec": recommendation_id, "loc": location_id,
         "v": vendor_id, "st": status, "cur": currency, "total": total, "edd": expected_delivery_date,
         "key": idempotency_key, "appr": approval_id, "by": created_by, "notes": notes},
    )
    await session.execute(
        text(
            """INSERT INTO order_lines (order_line_id, tenant_id, order_id, line_no, product_id, sku, ordered_qty,
                   unit_cost, line_total) VALUES (:lid, :t, :oid, 1, :pid, :sku, :q, :c, :lt)"""
        ),
        {"lid": uuid4(), "t": tenant_id, "oid": order_id, "pid": product_id, "sku": sku, "q": quantity,
         "c": unit_cost, "lt": total},
    )
    return order_id


async def update_status(session: AsyncSession, order_id: UUID, *, status: str | None = None,
                        payment_status: str | None = None, expected_version: int | None = None) -> None:
    result = await session.execute(
        text(
            """UPDATE orders SET status = COALESCE(:s, status), payment_status = COALESCE(:ps, payment_status),
                      version = version + 1
               WHERE order_id = :id AND (CAST(:v AS INTEGER) IS NULL OR version = :v)"""
        ),
        {"s": status, "ps": payment_status, "id": order_id, "v": expected_version},
    )
    if result.rowcount == 0:
        raise Conflict("order was modified concurrently", details={"order_id": str(order_id)})


async def update_quantity(session: AsyncSession, order_id: UUID, quantity: int, expected_version: int) -> None:
    line = (
        await session.execute(
            text("SELECT unit_cost FROM order_lines WHERE order_id = :id AND line_no = 1"), {"id": order_id}
        )
    ).one()
    total = (line.unit_cost * quantity).quantize(Decimal("0.01"))
    await session.execute(
        text("UPDATE order_lines SET ordered_qty = :q, line_total = :lt WHERE order_id = :id AND line_no = 1"),
        {"q": quantity, "lt": total, "id": order_id},
    )
    result = await session.execute(
        text(
            """UPDATE orders SET total_amount = (SELECT sum(line_total) FROM order_lines WHERE order_id = :id),
                      version = version + 1 WHERE order_id = :id AND version = :v"""
        ),
        {"id": order_id, "v": expected_version},
    )
    if result.rowcount == 0:
        raise Conflict("order was modified concurrently", details={"order_id": str(order_id)})


async def update_delivery_date(session: AsyncSession, order_id: UUID, when: date) -> None:
    await session.execute(text("UPDATE orders SET expected_delivery_date = :d WHERE order_id = :id"),
                          {"d": when, "id": order_id})


async def tenant_limits(session: AsyncSession, tenant_id: str) -> dict:
    row = (
        await session.execute(
            text("SELECT currency, approval_threshold, payment_limit FROM tenants WHERE tenant_id = :t"), {"t": tenant_id}
        )
    ).one()
    return dict(row._mapping)


async def product_for_recommendation(session: AsyncSession, recommendation_id: UUID) -> dict:
    row = (
        await session.execute(
            text("SELECT product_id, location_id, vendor_id FROM recommendations WHERE recommendation_id = :id"),
            {"id": recommendation_id},
        )
    ).one()
    return dict(row._mapping)
