from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, computed_field

from ordering.domain.schemas.common import Money, OutModel, StrictModel

RiskLevel = Literal["low", "medium", "high", "critical"]
RecStatus = Literal["open", "accepted", "ordered", "dismissed", "expired"]


class RecommendationFilter(StrictModel):
    status: RecStatus | None = "open"
    risk_levels: list[RiskLevel] | None = None
    sku: str | None = Field(None, pattern=r"^[A-Z0-9-]{3,32}$")
    location_code: str | None = Field(None, max_length=32)
    vendor_code: str | None = Field(None, max_length=32)
    category: str | None = Field(None, max_length=64)
    limit: int = Field(10, ge=1, le=50)


class Recommendation(OutModel):
    recommendation_id: UUID
    sku: str
    product_name: str
    category: str
    location_code: str
    location_name: str
    location_type: str
    vendor_id: UUID
    vendor_code: str
    vendor_name: str
    current_stock: int
    forecast_demand: Decimal
    forecast_horizon_days: int
    safety_stock: int
    recommended_qty: int
    unit_cost: Money
    reason: str
    risk_level: RiskLevel
    expected_delivery_date: date
    status: RecStatus
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estimated_total(self) -> Money:
        return (self.unit_cost * self.recommended_qty).quantize(Decimal("0.01"))


class InventoryPosition(OutModel):
    sku: str
    product_name: str
    location_code: str
    location_name: str
    on_hand: int
    on_order: int
    allocated: int
    safety_stock: int
    avg_daily_demand: Decimal
    days_of_cover: Decimal
    below_safety_stock: bool


class InventoryRisk(OutModel):
    sku: str
    product_name: str
    location_code: str
    on_hand: int
    safety_stock: int
    avg_daily_demand: Decimal
    days_of_cover: Decimal
    risk_level: RiskLevel
    open_recommendation_id: UUID | None = None


class Vendor(OutModel):
    vendor_id: UUID
    vendor_code: str
    name: str
    lead_time_days: int
    min_order_qty: int
    payment_terms: str
    rating: Decimal
    is_active: bool
