"""Client for the internal agent service. Forwards the verified bearer token (zero trust:
the agent service re-verifies), correlation/trace headers and the Idempotency-Key."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import Request
from starlette.responses import JSONResponse, Response

from ordering.common.config import get_settings
from ordering.common.errors import DependencyUnavailable
from ordering.common.http import get_http_client, propagation_headers


async def forward(request: Request, method: str, path: str, json: Any | None = None, params: dict | None = None) -> Response:
    settings = get_settings()
    headers = propagation_headers({"Authorization": request.headers.get("authorization", "")})
    if key := request.headers.get("idempotency-key"):
        headers["Idempotency-Key"] = key
    try:
        upstream = await get_http_client().request(
            method, f"{settings.agent_service_url}{path}", json=json, params=params, headers=headers,
            timeout=settings.agent_request_timeout_s,
        )
    except httpx.TimeoutException as exc:
        raise DependencyUnavailable("the assistant took too long to respond; your request may still complete - "
                                    "check the conversation before retrying", details={"dependency": "agent-service"}) from exc
    except httpx.HTTPError as exc:
        raise DependencyUnavailable("the assistant service is unavailable", details={"dependency": "agent-service"}) from exc
    content_type = upstream.headers.get("content-type", "application/json")
    response = Response(content=upstream.content, status_code=upstream.status_code, media_type=content_type)
    if retry := upstream.headers.get("retry-after"):
        response.headers["Retry-After"] = retry
    rl = getattr(request.state, "rate_limit", None)
    if rl:
        response.headers["X-RateLimit-Limit"], response.headers["X-RateLimit-Remaining"], response.headers["X-RateLimit-Reset"] = map(str, rl)
    return response


def json_response(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)
