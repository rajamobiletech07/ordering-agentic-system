"""Read access to recommendations, inventory and vendors (authoritative facts in PostgreSQL)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ordering.common.errors import NotFound
from ordering.domain.schemas.recommendation import (
    InventoryPosition,
    InventoryRisk,
    Recommendation,
    RecommendationFilter,
    Vendor,
)

_REC_SELECT = """
SELECT r.recommendation_id, r.sku, p.name AS product_name, p.category, l.location_code, l.name AS location_name,
       l.location_type, r.vendor_id, v.vendor_code, v.name AS vendor_name, r.current_stock, r.forecast_demand,
       r.forecast_horizon_days, r.safety_stock, r.recommended_qty, r.unit_cost, r.reason, r.risk_level,
       r.expected_delivery_date, r.status, r.created_at
FROM recommendations r
JOIN products p ON p.tenant_id = r.tenant_id AND p.product_id = r.product_id
JOIN locations l ON l.tenant_id = r.tenant_id AND l.location_id = r.location_id
JOIN vendors v ON v.tenant_id = r.tenant_id AND v.vendor_id = r.vendor_id
"""

_RISK_ORDER = "CASE r.risk_level WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"


async def list_recommendations(session: AsyncSession, f: RecommendationFilter) -> list[Recommendation]:
    rows = await session.execute(
        text(
            _REC_SELECT
            + """
            WHERE (CAST(:status AS TEXT) IS NULL OR r.status = :status)
              AND (CAST(:risks AS TEXT[]) IS NULL OR r.risk_level = ANY(CAST(:risks AS TEXT[])))
              AND (CAST(:sku AS TEXT) IS NULL OR r.sku = :sku)
              AND (CAST(:loc AS TEXT) IS NULL OR l.location_code = :loc)
              AND (CAST(:vendor AS TEXT) IS NULL OR v.vendor_code = :vendor)
              AND (CAST(:category AS TEXT) IS NULL OR lower(p.category) = lower(:category))
            ORDER BY """
            + _RISK_ORDER
            + """, (r.current_stock::numeric / GREATEST(r.safety_stock, 1)) ASC, r.created_at DESC
            LIMIT :limit"""
        ),
        {"status": f.status, "risks": f.risk_levels, "sku": f.sku, "loc": f.location_code, "vendor": f.vendor_code,
         "category": f.category, "limit": f.limit},
    )
    return [Recommendation.model_validate(dict(r._mapping)) for r in rows]


async def get_recommendation(session: AsyncSession, recommendation_id: UUID, for_update: bool = False) -> Recommendation:
    sql = _REC_SELECT + " WHERE r.recommendation_id = :id" + (" FOR UPDATE OF r" if for_update else "")
    row = (await session.execute(text(sql), {"id": recommendation_id})).first()
    if row is None:
        raise NotFound("recommendation not found", details={"recommendation_id": str(recommendation_id)})
    return Recommendation.model_validate(dict(row._mapping))


async def get_inventory_position(session: AsyncSession, sku: str, location_code: str) -> InventoryPosition:
    row = (
        await session.execute(
            text(
                """SELECT p.sku, p.name AS product_name, l.location_code, l.name AS location_name, i.on_hand, i.on_order,
                          i.allocated, i.safety_stock, i.avg_daily_demand,
                          round(i.on_hand / GREATEST(i.avg_daily_demand, 0.01), 1) AS days_of_cover,
                          (i.on_hand < i.safety_stock) AS below_safety_stock
                   FROM inventory_positions i
                   JOIN products p ON p.tenant_id = i.tenant_id AND p.product_id = i.product_id
                   JOIN locations l ON l.tenant_id = i.tenant_id AND l.location_id = i.location_id
                   WHERE p.sku = :sku AND l.location_code = :loc"""
            ),
            {"sku": sku, "loc": location_code},
        )
    ).first()
    if row is None:
        raise NotFound("no inventory position for sku/location", details={"sku": sku, "location_code": location_code})
    return InventoryPosition.model_validate(dict(row._mapping))


async def list_inventory_risks(
    session: AsyncSession, location_code: str | None = None, category: str | None = None, limit: int = 10
) -> list[InventoryRisk]:
    rows = await session.execute(
        text(
            """SELECT p.sku, p.name AS product_name, l.location_code, i.on_hand, i.safety_stock, i.avg_daily_demand,
                      round(i.on_hand / GREATEST(i.avg_daily_demand, 0.01), 1) AS days_of_cover,
                      CASE WHEN i.on_hand < i.safety_stock * 0.5 THEN 'critical'
                           WHEN i.on_hand < i.safety_stock THEN 'high'
                           WHEN i.on_hand < i.safety_stock * 1.5 THEN 'medium' ELSE 'low' END AS risk_level,
                      r.recommendation_id AS open_recommendation_id
               FROM inventory_positions i
               JOIN products p ON p.tenant_id = i.tenant_id AND p.product_id = i.product_id
               JOIN locations l ON l.tenant_id = i.tenant_id AND l.location_id = i.location_id
               LEFT JOIN recommendations r ON r.tenant_id = i.tenant_id AND r.product_id = i.product_id
                    AND r.location_id = i.location_id AND r.status = 'open'
               WHERE i.on_hand < i.safety_stock
                 AND (CAST(:loc AS TEXT) IS NULL OR l.location_code = :loc)
                 AND (CAST(:cat AS TEXT) IS NULL OR lower(p.category) = lower(:cat))
               ORDER BY (i.on_hand::numeric / GREATEST(i.safety_stock, 1)) ASC, i.avg_daily_demand DESC
               LIMIT :limit"""
        ),
        {"loc": location_code, "cat": category, "limit": limit},
    )
    return [InventoryRisk.model_validate(dict(r._mapping)) for r in rows]


async def get_vendor(session: AsyncSession, vendor_code: str | None = None, vendor_id: UUID | None = None) -> Vendor:
    row = (
        await session.execute(
            text(
                """SELECT vendor_id, vendor_code, name, lead_time_days, min_order_qty, payment_terms, rating, is_active
                   FROM vendors WHERE (CAST(:code AS TEXT) IS NULL OR vendor_code = :code)
                     AND (CAST(:vid AS UUID) IS NULL OR vendor_id = :vid) LIMIT 1"""
            ),
            {"code": vendor_code, "vid": vendor_id},
        )
    ).first()
    if row is None:
        raise NotFound("vendor not found")
    return Vendor.model_validate(dict(row._mapping))


async def set_status(session: AsyncSession, recommendation_id: UUID, status: str) -> None:
    await session.execute(
        text("UPDATE recommendations SET status = :s WHERE recommendation_id = :id"),
        {"s": status, "id": recommendation_id},
    )
