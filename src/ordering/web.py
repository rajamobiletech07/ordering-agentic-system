"""Shared FastAPI plumbing: request context/correlation middleware, metrics, and RFC 9457
``application/problem+json`` error responses."""

from __future__ import annotations

import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from ordering.common.context import RequestContext, reset_context, set_context
from ordering.common.errors import OrderingError
from ordering.common.logging import get_logger
from ordering.common.metrics import REQUEST_LATENCY, REQUESTS
from ordering.common.tracing import current_trace_id

log = get_logger("ordering.web")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def install_request_context(app: FastAPI, service: str) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        ctx = RequestContext(request_id=request_id, conversation_id=request.headers.get("x-conversation-id"))
        request.state.ctx = ctx
        token = set_context(ctx)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = request_id
            trace_id = current_trace_id()
            if trace_id:
                response.headers["x-trace-id"] = trace_id
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            if path not in ("/healthz", "/readyz", "/metrics"):
                elapsed = time.perf_counter() - start
                REQUESTS.labels(service, path, request.method, str(status)).inc()
                REQUEST_LATENCY.labels(service, path).observe(elapsed)
                log.info("http_request", method=request.method, path=path, status=status,
                         ms=round(elapsed * 1000, 1), request_id=request_id)
            reset_context(token)


def problem(status: int, code: str, title: str, detail: str, request: Request, **extra) -> JSONResponse:
    retry_after = extra.pop("retry_after", 5)
    body = {
        "type": f"https://errors.ordering.example/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "instance": str(request.url.path),
        "request_id": getattr(getattr(request.state, "ctx", None), "request_id", None),
        **extra,
    }
    headers = {"Retry-After": str(retry_after)} if status in (429, 503) else None
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(OrderingError)
    async def ordering_error(request: Request, exc: OrderingError):
        if exc.status_code >= 500:
            log.warning("request_failed", code=exc.code, error=exc.message)
        return problem(exc.status_code, exc.code, exc.code.replace("_", " ").title(), exc.message, request,
                       retryable=exc.retryable, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return problem(422, "validation_failed", "Validation Failed", "request validation failed", request,
                       details={"errors": errors})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled_exception", path=request.url.path)
        return problem(500, "internal_error", "Internal Error", "an unexpected error occurred", request)
