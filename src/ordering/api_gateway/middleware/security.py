"""Edge middleware: body-size limit, security headers, per-principal rate limiting."""

from __future__ import annotations

import time

from fastapi import Depends, FastAPI, Request
from starlette.responses import JSONResponse

from ordering.common.config import get_settings
from ordering.common.context import Principal
from ordering.common.errors import RateLimited
from ordering.common.logging import get_logger
from ordering.common.redis import get_redis
from ordering.security.dependencies import get_principal

log = get_logger("ordering.api.middleware")


def install_edge_middleware(app: FastAPI) -> None:
    settings = get_settings()

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            return JSONResponse({"type": "https://errors.ordering.example/payload_too_large", "title": "Payload Too Large",
                                 "status": 413, "code": "payload_too_large",
                                 "detail": f"request body exceeds {settings.max_body_bytes} bytes"},
                                status_code=413, media_type="application/problem+json")
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        return response


async def rate_limited(request: Request, principal: Principal = Depends(get_principal)) -> Principal:
    """Fixed-window limiter per tenant+user (Redis). Fails open if Redis is unavailable."""
    limit = get_settings().rate_limit_per_minute
    window = int(time.time() // 60)
    key = f"rl:{principal.tenant_id}:{principal.user_id}:{window}"
    try:
        r = get_redis()
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 65)
    except Exception as exc:
        log.warning("rate_limiter_unavailable", error=str(exc)[:100])
        return principal
    request.state.rate_limit = (limit, max(0, limit - count), 60 - int(time.time() % 60))
    if count > limit:
        raise RateLimited("rate limit exceeded", details={"limit_per_minute": limit, "retry_after_s": 60 - int(time.time() % 60)})
    return principal
