"""Shared async HTTP client with trace/correlation propagation.

Replaces mcp-core's per-call ``aiohttp.ClientSession`` + global header dict: one pooled
client per process, explicit timeouts, and per-request headers only.
"""

from __future__ import annotations

import httpx

from ordering.common.context import get_context
from ordering.common.tracing import inject_headers

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=3.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


def propagation_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    ctx = get_context()
    headers = {"x-request-id": ctx.request_id}
    if ctx.conversation_id:
        headers["x-conversation-id"] = ctx.conversation_id
    headers.update(extra or {})
    return inject_headers(headers)


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
