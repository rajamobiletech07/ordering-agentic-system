"""MCP tool boundary: authentication, agent tool-access authorization, context, tracing.

Adapted from mcp-core ``validate_auth_decorator`` (inventory_smart_server/auth_decorator.py),
with these fixes:

* fastmcp >= 4 strips ``Authorization`` from ``get_http_headers()`` by default - we opt in
  explicitly with ``include={"authorization", ...}``;
* tokens are verified locally (no per-call network round-trip);
* the token must be an internal *service* token naming the acting agent, and the pair
  (agent, user roles) must be allowed by :data:`TOOL_POLICIES` - enforced here, outside the LLM;
* the verified tenant is placed in the request context; tools never accept ``tenant_id``
  as an argument, so a prompt-injected tool call cannot cross tenants.
"""

from __future__ import annotations

import functools
import inspect
import time

from opentelemetry import context as otel_context

from ordering.common.context import RequestContext, reset_context, set_context
from ordering.common.errors import AuthenticationError
from ordering.common.faults import maybe_inject_fault
from ordering.common.logging import get_logger
from ordering.common.metrics import TOOL_CALLS, TOOL_LATENCY
from ordering.common.tracing import extract_context, tracer
from ordering.decorators.errors import handle_errors
from ordering.security.permissions import authorize_tool
from ordering.security.service_tokens import verify_service_token

log = get_logger("ordering.mcp")

_FORWARDED = {"authorization", "x-request-id", "x-conversation-id", "traceparent", "tracestate"}


def _request_headers() -> dict[str, str]:
    from fastmcp.server.dependencies import get_http_headers

    return get_http_headers(include=_FORWARDED)


def agent_tool(server: str, tool: str):
    """Decorate an async MCP tool implementation. Apply *below* ``@mcp.tool(...)``."""
    qualified = f"{server}.{tool}"

    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("agent_tool only supports async functions")

        @handle_errors("mcp")
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            headers = _request_headers()
            token = headers.get("authorization", "")
            if not token:
                raise AuthenticationError("missing service token")
            principal = verify_service_token(token)
            authorize_tool(qualified, principal.actor, principal.roles)

            ctx_token = set_context(
                RequestContext(
                    request_id=headers.get("x-request-id") or RequestContext().request_id,
                    principal=principal,
                    agent=principal.actor,
                    conversation_id=headers.get("x-conversation-id"),
                )
            )
            otel_token = otel_context.attach(extract_context(headers))
            start, outcome = time.perf_counter(), "ok"
            try:
                with tracer("ordering.mcp").start_as_current_span(f"mcp.tool {qualified}") as span:
                    span.set_attribute("gen_ai.tool.name", qualified)
                    span.set_attribute("gen_ai.agent.name", principal.actor or "")
                    span.set_attribute("ordering.tenant_id", principal.tenant_id)
                    await maybe_inject_fault(server)
                    return await func(*args, **kwargs)
            except Exception as exc:
                outcome = getattr(exc, "code", exc.__class__.__name__)
                raise
            finally:
                elapsed = time.perf_counter() - start
                TOOL_CALLS.labels(server, tool, principal.actor or "", outcome).inc()
                TOOL_LATENCY.labels(server, tool).observe(elapsed)
                log.info("tool_call", tool=qualified, outcome=outcome, ms=round(elapsed * 1000, 1))
                otel_context.detach(otel_token)
                reset_context(ctx_token)

        return wrapper

    return decorator
