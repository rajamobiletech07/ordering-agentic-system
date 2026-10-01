"""Fault injection for resilience testing (disabled in production).

Faults come from ``FAULT_INJECTION`` (static) or Redis keys ``faults:<target>`` (runtime,
set via the admin-only ``POST /v1/dev/faults`` endpoint, with TTL). Modes:

* ``unavailable`` - raise :class:`DependencyUnavailable` (simulates a down dependency)
* ``slow``        - sleep past the caller's timeout
* ``flaky``       - fail ~50% of calls
"""

from __future__ import annotations

import asyncio
import random

from ordering.common.config import get_settings
from ordering.common.errors import DependencyUnavailable
from ordering.common.logging import get_logger

log = get_logger("ordering.faults")
VALID_MODES = {"unavailable", "slow", "flaky"}


async def active_fault(target: str) -> str | None:
    settings = get_settings()
    if settings.env == "production":
        return None
    for flag in settings.faults():
        name, _, mode = flag.partition(":")
        if name == target and mode in VALID_MODES:
            return mode
    try:
        from ordering.common.redis import get_redis

        value = await get_redis().get(f"faults:{target}")
        return value if isinstance(value, str) and value in VALID_MODES else None
    except Exception:
        return None


async def maybe_inject_fault(target: str) -> None:
    mode = await active_fault(target)
    if mode is None:
        return
    log.warning("fault_injected", target=target, mode=mode)
    if mode == "unavailable" or (mode == "flaky" and random.random() < 0.5):
        raise DependencyUnavailable(f"{target} is unavailable (injected fault)", details={"dependency": target})
    if mode == "slow":
        await asyncio.sleep(30)


async def set_fault(target: str, mode: str | None, ttl_s: int = 600) -> None:
    from ordering.common.redis import get_redis

    if mode is None:
        await get_redis().delete(f"faults:{target}")
    else:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        await get_redis().set(f"faults:{target}", mode, ex=ttl_s)
