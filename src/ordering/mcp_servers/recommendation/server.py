"""Recommendation MCP server (read-only): replenishment recommendations, inventory, vendors.

Tool pattern (from mcp-core ``mcp_tools_new/*.py``): ``@mcp.tool`` + a boundary decorator +
typed pydantic input + docstring used for the schema. Tenant comes from the verified token.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from ordering.common.context import require_principal
from ordering.common.db import tenant_session
from ordering.decorators.tool_access import agent_tool
from ordering.domain.schemas.recommendation import RecommendationFilter
from ordering.mcp_servers.common import create_mcp, serve
from ordering.repositories import recommendations as repo

mcp = create_mcp(
    "recommendation-mcp",
    "Replenishment recommendations and supporting inventory/vendor facts. Read-only; tenant scoped.",
)
READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


@mcp.tool(name="list_recommendations", annotations=READ_ONLY)
@agent_tool("recommendation", "list_recommendations")
async def list_recommendations(filters: RecommendationFilter) -> dict:
    """List replenishment recommendations, highest risk first.

    Args:
        filters: status (default ``open``), risk_levels, sku, location_code, vendor_code, category, limit.
    Returns:
        ``{"recommendations": [...], "count": n}`` with stock, forecast, safety stock, recommended quantity,
        unit cost, estimated total, reason and expected delivery date.
    """
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        recs = await repo.list_recommendations(s, filters)
    return {"recommendations": [r.model_dump(mode="json") for r in recs], "count": len(recs)}


@mcp.tool(name="get_recommendation", annotations=READ_ONLY)
@agent_tool("recommendation", "get_recommendation")
async def get_recommendation(recommendation_id: UUID) -> dict:
    """Get one recommendation by ID with full supporting detail."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        rec = await repo.get_recommendation(s, recommendation_id)
    return rec.model_dump(mode="json")


@mcp.tool(name="get_inventory_position", annotations=READ_ONLY)
@agent_tool("recommendation", "get_inventory_position")
async def get_inventory_position(
    sku: str = Field(..., pattern=r"^[A-Z0-9-]{3,32}$"),
    location_code: str = Field(..., max_length=32),
) -> dict:
    """Current on-hand, on-order, safety stock, average daily demand and days of cover for a SKU at a location."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        pos = await repo.get_inventory_position(s, sku, location_code)
    return pos.model_dump(mode="json")


@mcp.tool(name="list_inventory_risks", annotations=READ_ONLY)
@agent_tool("recommendation", "list_inventory_risks")
async def list_inventory_risks(
    location_code: str | None = Field(None, max_length=32),
    category: str | None = Field(None, max_length=64),
    limit: int = Field(10, ge=1, le=50),
) -> dict:
    """Items below safety stock (stock-out risk), most severe first, with any open recommendation."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        risks = await repo.list_inventory_risks(s, location_code, category, limit)
    return {"risks": [r.model_dump(mode="json") for r in risks], "count": len(risks)}


@mcp.tool(name="get_vendor", annotations=READ_ONLY)
@agent_tool("recommendation", "get_vendor")
async def get_vendor(vendor_code: str = Field(..., max_length=32)) -> dict:
    """Vendor lead time, minimum order quantity, payment terms and rating."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        vendor = await repo.get_vendor(s, vendor_code=vendor_code)
    return vendor.model_dump(mode="json")



def main() -> None:
    serve(mcp, "mcp-recommendation", 9001)


if __name__ == "__main__":
    main()
