"""Bounded retries, timeouts and circuit breaking.

Retry policy is applied at exactly ONE layer per dependency (see ARCHITECTURE.md "retry
matrix"). ``with_retry`` refuses to retry a mutation unless an idempotency key is present, so a
timed-out-but-committed write can never be executed twice.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ordering.common.errors import CircuitOpen, DependencyTimeout, DependencyUnavailable
from ordering.common.logging import get_logger

log = get_logger("ordering.resilience")

DEFAULT_RETRYABLE: tuple[type[BaseException], ...] = (DependencyUnavailable, ConnectionError, asyncio.TimeoutError)


def with_timeout(seconds: float):
    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("with_timeout only supports async functions")

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
            except TimeoutError as exc:
                raise DependencyTimeout(f"{func.__qualname__} exceeded {seconds}s") from exc

        return wrapper

    return decorator


def with_retry(
    attempts: int = 3,
    *,
    base_delay: float = 0.2,
    max_delay: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE,
    mutation: bool = False,
    idempotency_key_param: str = "idempotency_key",
    should_retry: Callable[[BaseException], bool] | None = None,
):
    """Retry with exponential backoff + full jitter.

    For ``mutation=True`` the call is retried only when ``idempotency_key_param`` is
    present and non-empty in the call's arguments; otherwise it runs exactly once.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("with_retry only supports async functions")
        sig = inspect.signature(func)

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            max_attempts = attempts
            if mutation:
                bound = sig.bind_partial(*args, **kwargs)
                key = bound.arguments.get(idempotency_key_param)
                if key is None and "arguments" in bound.arguments and isinstance(bound.arguments["arguments"], dict):
                    key = bound.arguments["arguments"].get(idempotency_key_param)
                if not key:
                    max_attempts = 1
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retry_on as exc:
                    retryable = getattr(exc, "retryable", True) and (should_retry(exc) if should_retry else True)
                    if attempt >= max_attempts or not retryable:
                        raise
                    delay = random.uniform(0, min(max_delay, base_delay * 2 ** (attempt - 1)))
                    log.warning("retrying", fn=func.__qualname__, attempt=attempt, delay=round(delay, 3),
                                error=exc.__class__.__name__)
                    await asyncio.sleep(delay)

        return wrapper

    return decorator


@dataclass
class CircuitBreaker:
    """Minimal async circuit breaker (closed → open → half-open)."""

    name: str
    failure_threshold: int = 5
    reset_timeout_s: float = 30.0
    _failures: int = 0
    _opened_at: float | None = None
    _half_open_inflight: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.reset_timeout_s:
            return "half_open"
        return "open"

    async def before_call(self) -> None:
        async with self._lock:
            state = self.state
            if state == "open":
                raise CircuitOpen(f"circuit '{self.name}' is open", details={"dependency": self.name})
            if state == "half_open":
                if self._half_open_inflight:
                    raise CircuitOpen(f"circuit '{self.name}' is half-open", details={"dependency": self.name})
                self._half_open_inflight = True

    async def on_success(self) -> None:
        async with self._lock:
            self._failures, self._opened_at, self._half_open_inflight = 0, None, False

    async def on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            self._half_open_inflight = False
            if self._failures >= self.failure_threshold or self._opened_at is not None:
                self._opened_at = time.monotonic()
                log.warning("circuit_opened", circuit=self.name, failures=self._failures)

    def reset(self) -> None:
        self._failures, self._opened_at, self._half_open_inflight = 0, None, False


def circuit(breaker: CircuitBreaker, failure_on: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE):
    """Guard an async call with a circuit breaker. Only ``failure_on`` errors trip it."""

    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("circuit only supports async functions")

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            await breaker.before_call()
            try:
                result = await func(*args, **kwargs)
            except failure_on:
                await breaker.on_failure()
                raise
            except BaseException:
                await breaker.on_success()  # business errors mean the dependency is up
                raise
            await breaker.on_success()
            return result

        return wrapper

    return decorator
