"""Shared async Redis client (rate limiting, thread cache, runtime fault flags)."""

from __future__ import annotations

import redis.asyncio as aioredis

from ordering.common.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=2,
                                    socket_connect_timeout=2, health_check_interval=30)
    return _client


def set_redis(client: aioredis.Redis | None) -> None:
    """Test hook (e.g. fakeredis)."""
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
