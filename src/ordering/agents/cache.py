"""Per-chat-thread cache with mandatory cleanup.

Namespace: ``cache:{tenant}:{thread}:{kind}:{key}`` where ``kind`` is ``llm`` (deterministic LLM
routes), ``tool`` (read-only recommendation tools) or ``rag`` (retrieval results).

Order / payment / notification data is never cached - it is always read from the system of record.

Cleanup is mandatory and layered:
  1. every key has a TTL, refreshed (sliding) on access;
  2. ``invalidate(kind)`` after any mutation in the thread (e.g. an order was created);
  3. ``clear()`` when the thread is closed/deleted, the workflow completes, or memory is erased;
  4. :func:`sweep_orphans` periodically removes namespaces idle past the TTL (index sets included).
"""

from __future__ import annotations

import json
import time
from typing import Any

from ordering.common.config import get_settings
from ordering.common.logging import get_logger
from ordering.common.metrics import CACHE_EVENTS
from ordering.common.redis import get_redis

log = get_logger("ordering.cache")
THREADS_ZSET = "cache:threads"
KINDS = ("llm", "tool", "rag")


class ThreadCache:
    def __init__(self, tenant_id: str, thread_id: str, ttl_s: int | None = None):
        self.tenant_id, self.thread_id = tenant_id, thread_id
        self.ns = f"cache:{tenant_id}:{thread_id}"
        self.ttl = ttl_s or get_settings().thread_cache_ttl_s

    def _key(self, kind: str, key: str) -> str:
        return f"{self.ns}:{kind}:{key}"

    def _index(self, kind: str) -> str:
        return f"{self.ns}:__index__:{kind}"

    async def get(self, kind: str, key: str) -> Any | None:
        try:
            r = get_redis()
            raw = await r.get(self._key(kind, key))
            if raw is None:
                CACHE_EVENTS.labels(kind, "miss").inc()
                return None
            await r.expire(self._key(kind, key), self.ttl)  # sliding TTL
            await r.zadd(THREADS_ZSET, {self.ns: time.time()})
            CACHE_EVENTS.labels(kind, "hit").inc()
            return json.loads(raw)
        except Exception as exc:  # cache is best-effort; never break a request
            log.debug("cache_get_failed", error=str(exc)[:100])
            return None

    async def set(self, kind: str, key: str, value: Any, ttl_s: int | None = None) -> None:
        try:
            r = get_redis()
            ttl = min(ttl_s or self.ttl, self.ttl)
            pipe = r.pipeline()
            pipe.set(self._key(kind, key), json.dumps(value, default=str), ex=ttl)
            pipe.sadd(self._index(kind), key)
            pipe.expire(self._index(kind), self.ttl)
            pipe.zadd(THREADS_ZSET, {self.ns: time.time()})
            await pipe.execute()
            CACHE_EVENTS.labels(kind, "set").inc()
        except Exception as exc:
            log.debug("cache_set_failed", error=str(exc)[:100])

    async def invalidate(self, *kinds: str) -> int:
        removed = 0
        r = get_redis()
        for kind in kinds or KINDS:
            keys = await r.smembers(self._index(kind))
            if keys:
                removed += await r.delete(*[self._key(kind, k) for k in keys])
            await r.delete(self._index(kind))
            CACHE_EVENTS.labels(kind, "invalidate").inc()
        return removed

    async def clear(self, reason: str) -> int:
        """Remove every key in this thread's namespace."""
        r = get_redis()
        removed = 0
        async for key in r.scan_iter(match=f"{self.ns}:*", count=200):
            removed += await r.delete(key)
        await r.zrem(THREADS_ZSET, self.ns)
        CACHE_EVENTS.labels("all", "clear").inc()
        log.info("thread_cache_cleared", thread=self.thread_id, reason=reason, keys=removed)
        return removed

    async def size(self) -> int:
        count = 0
        async for _ in get_redis().scan_iter(match=f"{self.ns}:*", count=200):
            count += 1
        return count


async def clear_user_threads(tenant_id: str, thread_ids: list[str], reason: str) -> int:
    total = 0
    for thread_id in thread_ids:
        total += await ThreadCache(tenant_id, thread_id).clear(reason)
    return total


async def sweep_orphans(max_idle_s: int | None = None) -> int:
    """Clear namespaces idle for longer than the TTL (index sets, stragglers)."""
    r = get_redis()
    cutoff = time.time() - (max_idle_s or get_settings().thread_cache_ttl_s)
    stale = await r.zrangebyscore(THREADS_ZSET, 0, cutoff)
    for ns in stale:
        async for key in r.scan_iter(match=f"{ns}:*", count=200):
            await r.delete(key)
        await r.zrem(THREADS_ZSET, ns)
        CACHE_EVENTS.labels("all", "sweep").inc()
    if stale:
        log.info("thread_cache_sweep", namespaces=len(stale))
    return len(stale)
