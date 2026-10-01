"""API Gateway (public edge, port 8000).

Authentication (JWT bearer), authorization (role permissions), request validation (pydantic,
body-size limits), per-user rate limiting (Redis), request tracing (X-Request-ID, W3C trace
context, OTel), security headers, RFC 9457 errors. Business logic lives in the agent service.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text

from ordering.api_gateway.middleware.security import install_edge_middleware
from ordering.api_gateway.routes.v1 import router as v1_router
from ordering.common.config import get_settings
from ordering.common.db import admin_session, dispose_engines
from ordering.common.errors import NotFound, PermissionDenied
from ordering.common.http import close_http_client, get_http_client
from ordering.common.logging import configure_logging
from ordering.common.metrics import metrics_response
from ordering.common.redis import close_redis, get_redis
from ordering.common.tracing import setup_tracing
from ordering.security.jwt import issue_dev_token
from ordering.web import install_error_handlers, install_request_context


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_http_client()
    await close_redis()
    await dispose_engines()


settings = get_settings()
settings.service_name = "api-gateway"
configure_logging("api-gateway", settings.log_level)
app = FastAPI(
    title="Ordering Assistant API",
    version=settings.agent_version,
    description="Agentic replenishment-ordering chatbot. Authenticate with `Authorization: Bearer <JWT>`.",
    lifespan=lifespan,
)
install_request_context(app, "api-gateway")
install_edge_middleware(app)
install_error_handlers(app)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3001"], allow_methods=["GET", "POST", "DELETE"],
                   allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"])
app.include_router(v1_router)
setup_tracing(settings, app=app)


@app.get("/healthz", tags=["ops"])
async def healthz():
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz():
    checks = {}
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc.__class__.__name__}"
    try:
        r = await get_http_client().get(f"{settings.agent_service_url}/readyz", timeout=3)
        checks["agent_service"] = "ok" if r.status_code == 200 else f"status {r.status_code}"
    except Exception as exc:
        checks["agent_service"] = f"error: {exc.__class__.__name__}"
    ready = all(v == "ok" for v in checks.values())
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ready" if ready else "degraded", "checks": checks}, status_code=200 if ready else 503)


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


class DevTokenRequest(BaseModel):
    user_id: str = Field(..., examples=["u-acme-buyer"])
    ttl_s: int = Field(3600, ge=60, le=86_400)


@app.post("/v1/auth/dev-token", tags=["auth (local only)"],
          summary="Issue a JWT for a seeded user - local development only, disabled in production")
async def dev_token(body: DevTokenRequest, request: Request):
    if not settings.enable_dev_token_endpoint or settings.env == "production":
        raise PermissionDenied("dev token endpoint is disabled")
    async with admin_session() as s:
        row = (await s.execute(text("SELECT tenant_id, user_id, email, roles FROM users WHERE user_id = :u AND is_active"),
                               {"u": body.user_id})).first()
    if row is None:
        raise NotFound("unknown user")
    token = issue_dev_token(row.user_id, row.tenant_id, list(row.roles), row.email, body.ttl_s)
    token_type = "Bearer"  # noqa: S105  # nosec B105 - OAuth token type name, not a secret
    return {"access_token": token, "token_type": token_type, "expires_in": body.ttl_s, "tenant_id": row.tenant_id,
            "roles": list(row.roles)}
