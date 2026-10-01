"""Shared MCP server scaffolding.

Adapted from mcp-core ``inventory_smart_server/fastmcp_server.py`` (FastMCP app + health route
served by uvicorn) and ``streamable_http_template``: streamable HTTP transport at ``/mcp``,
plus ``/healthz`` (liveness), ``/readyz`` (DB reachable) and ``/metrics`` (Prometheus).
Servers run stateless so they scale horizontally behind a load balancer.
"""

from __future__ import annotations

import uvicorn
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from ordering.common.config import get_settings
from ordering.common.db import ping
from ordering.common.logging import configure_logging
from ordering.common.metrics import metrics_response
from ordering.common.tracing import setup_tracing


def create_mcp(name: str, instructions: str) -> FastMCP:
    mcp = FastMCP(name, instructions=instructions, version=get_settings().agent_version, mask_error_details=False)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": name})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        try:
            await ping()
            return JSONResponse({"status": "ready", "service": name})
        except Exception as exc:  # pragma: no cover - exercised in integration
            return JSONResponse({"status": "not_ready", "error": exc.__class__.__name__}, status_code=503)

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(_: Request):
        return metrics_response()

    return mcp


def build_app(mcp: FastMCP):
    return mcp.http_app(path="/mcp", stateless_http=True, json_response=True)


def serve(mcp: FastMCP, service_name: str, port: int) -> None:
    settings = get_settings()
    settings.service_name = service_name
    configure_logging(service_name, settings.log_level)
    setup_tracing(settings)
    uvicorn.run(build_app(mcp), host="0.0.0.0", port=port, log_level="warning")  # noqa: S104  # nosec B104 - container bind
