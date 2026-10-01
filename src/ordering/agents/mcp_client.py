"""Agent-side MCP client with tool authorization, retries, timeouts and circuit breaking.

Adapted from mcp-core ``streamable_http_template/server_usage_template.py`` (fastmcp ``Client`` +
``call_tool``), hardened for production:

* **Authorization outside the LLM** - :func:`authorize_tool` checks the agent → tool allowlist and
  the user's permissions *before* calling; the MCP server re-checks with the service token.
* **Per-call service token** carrying the verified user/tenant and the acting agent.
* **Retry policy (single layer)** - read tools: 3 attempts with backoff; mutations: 2 attempts
  only when an ``idempotency_key`` is present, otherwise exactly once.
* **Circuit breaker per MCP server** - an unavailable server fails fast (``circuit_open``).
* **Timeouts** per call; W3C trace context and request IDs are propagated.
* **Thread cache** for read-only recommendation tools; invalidated after any mutation.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from ordering.agents.cache import ThreadCache
from ordering.common.config import get_settings
from ordering.common.context import get_context, require_principal
from ordering.common.errors import (
    DependencyTimeout,
    DependencyUnavailable,
    OrderingError,
    ValidationFailed,
    error_from_dict,
)
from ordering.common.http import propagation_headers
from ordering.common.ids import sha256_hex
from ordering.common.logging import get_logger
from ordering.common.tracing import tracer
from ordering.decorators.resilience import CircuitBreaker
from ordering.security.permissions import authorize_tool
from ordering.security.service_tokens import mint_service_token

log = get_logger("ordering.mcp.client")
CACHEABLE_SERVERS = {"recommendation"}
READ_TIMEOUT_S, WRITE_TIMEOUT_S = 8.0, 15.0


def _has_idempotency_key(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("idempotency_key")) or any(_has_idempotency_key(v) for v in value.values())
    return False


def _parse_tool_error(exc: ToolError) -> OrderingError:
    message = str(exc)
    try:
        payload = json.loads(message)
        if isinstance(payload, dict) and "code" in payload:
            return error_from_dict(payload)
    except ValueError:
        pass
    if "validation error" in message.lower():
        return ValidationFailed("tool input validation failed", details={"error": message[:500]})
    return OrderingError(message[:300])


class MCPToolClient:
    def __init__(self) -> None:
        s = get_settings()
        self.urls = {
            "recommendation": s.mcp_recommendation_url,
            "ordering": s.mcp_ordering_url,
            "payment": s.mcp_payment_url,
            "notification": s.mcp_notification_url,
        }
        self.breakers = {name: CircuitBreaker(name, failure_threshold=5, reset_timeout_s=10) for name in self.urls}

    async def call(self, qualified: str, arguments: dict[str, Any], *, cache: ThreadCache | None = None) -> dict[str, Any]:
        principal = require_principal()
        agent = get_context().agent
        policy = authorize_tool(qualified, agent, principal.roles)
        server, tool = policy.server, policy.tool

        cache_key = None
        if cache is not None and not policy.mutation and server in CACHEABLE_SERVERS:
            cache_key = sha256_hex({"t": qualified, "a": arguments})
            cached = await cache.get("tool", cache_key)
            if cached is not None:
                return cached

        if policy.mutation:
            attempts = 2 if _has_idempotency_key(arguments) else 1
            timeout = WRITE_TIMEOUT_S
        else:
            attempts, timeout = 3, READ_TIMEOUT_S
        breaker = self.breakers[server]

        with tracer("ordering.agents").start_as_current_span(f"mcp.call {qualified}") as span:
            span.set_attribute("gen_ai.tool.name", qualified)
            span.set_attribute("gen_ai.agent.name", agent or "")
            for attempt in range(1, attempts + 1):
                started = time.perf_counter()
                try:
                    await breaker.before_call()
                    result = await self._invoke(server, tool, arguments, timeout)
                    await breaker.on_success()
                    log.info("mcp_call", tool=qualified, attempt=attempt, ms=round((time.perf_counter() - started) * 1000, 1))
                    break
                except DependencyUnavailable as exc:
                    if exc.code != "circuit_open":
                        await breaker.on_failure()
                    log.warning("mcp_call_unavailable", tool=qualified, attempt=attempt, error=exc.message[:160])
                    if attempt >= attempts or exc.code == "circuit_open":
                        span.set_attribute("error.type", exc.code)
                        raise
                    await asyncio.sleep(random.uniform(0.1, 0.4) * 2 ** (attempt - 1))
                except OrderingError:
                    await breaker.on_success()  # a business error means the server is healthy
                    raise

        if policy.mutation and cache is not None:
            await cache.invalidate("tool")
        elif cache_key is not None:
            await cache.set("tool", cache_key, result, ttl_s=300)
        return result

    async def _invoke(self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        ctx = get_context()
        token = mint_service_token(require_principal(), ctx.agent or "unknown", ctx.conversation_id)
        transport = StreamableHttpTransport(self.urls[server], headers=propagation_headers({"Authorization": f"Bearer {token}"}))
        try:
            async with asyncio.timeout(timeout_s):
                async with Client(transport, timeout=timeout_s, init_timeout=timeout_s) as client:
                    response = await client.call_tool(tool, arguments, timeout=timeout_s)
        except ToolError as exc:
            err = _parse_tool_error(exc)
            if isinstance(err, DependencyUnavailable):
                raise err from exc
            raise err from exc
        except TimeoutError as exc:
            raise DependencyTimeout(f"{server}.{tool} timed out after {timeout_s}s", details={"dependency": server}) from exc
        except OrderingError:
            raise
        except Exception as exc:  # connection refused, DNS, protocol errors → dependency unavailable
            raise DependencyUnavailable(f"MCP server '{server}' is unavailable",
                                        details={"dependency": server, "error": exc.__class__.__name__}) from exc
        data = response.data if response.data is not None else response.structured_content
        return data if isinstance(data, dict) else {"result": data}


_client: MCPToolClient | None = None


def get_mcp_client() -> MCPToolClient:
    global _client
    if _client is None:
        _client = MCPToolClient()
    return _client


def set_mcp_client(client: MCPToolClient | None) -> None:
    global _client
    _client = client
