"""Tracing, timing and audit decorators.

Generalizes mcp-core ``log_mcp_tool`` (inventory_smart_server/decorator.py): still async-only
and ``functools.wraps``-based (so FastAPI/FastMCP see the original signature), but inputs and
results are redacted, spans carry GenAI/tool attributes, and audit events are persisted.
"""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Callable
from typing import Any

from opentelemetry.trace import Status, StatusCode

from ordering.common.context import get_context
from ordering.common.logging import get_logger
from ordering.common.redaction import redact
from ordering.common.tracing import tracer

log = get_logger("ordering.observability")


def _ensure_async(func: Callable) -> None:
    if not inspect.iscoroutinefunction(func):
        raise TypeError(f"{func.__qualname__}: decorator only supports async functions")


def _summarize(value: Any, limit: int = 800) -> str:
    text = repr(redact(value if not hasattr(value, "model_dump") else value.model_dump(mode="json")))
    return text if len(text) <= limit else text[:limit] + "…"


def traced(span_name: str | None = None, **static_attrs: Any):
    """Wrap an async function in an OpenTelemetry span; records exceptions and status."""

    def decorator(func):
        _ensure_async(func)
        name = span_name or func.__qualname__

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            ctx = get_context()
            with tracer().start_as_current_span(name) as span:
                span.set_attribute("ordering.request_id", ctx.request_id)
                if ctx.tenant_id:
                    span.set_attribute("ordering.tenant_id", ctx.tenant_id)
                if ctx.agent:
                    span.set_attribute("gen_ai.agent.name", ctx.agent)
                for k, v in static_attrs.items():
                    span.set_attribute(k, v)
                try:
                    result = await func(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
                    raise

        return wrapper

    return decorator


def timed(metric: Any | None = None, **labels: str):
    """Log execution time (and observe a Prometheus histogram when given)."""

    def decorator(func):
        _ensure_async(func)

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            outcome = "ok"
            try:
                return await func(*args, **kwargs)
            except Exception:
                outcome = "error"
                raise
            finally:
                elapsed = time.perf_counter() - start
                if metric is not None:
                    metric.labels(**labels).observe(elapsed)
                log.debug("timed", fn=func.__qualname__, seconds=round(elapsed, 4), outcome=outcome)

        return wrapper

    return decorator


def audited(action: str, resource: Callable[[dict[str, Any], Any], str | None] | None = None):
    """Persist an audit event (who/what/when/outcome) for the decorated operation.

    ``resource`` receives ``(bound_arguments, result)`` and returns a resource identifier.
    Audit write failures are logged but never mask the business result.
    """

    def decorator(func):
        _ensure_async(func)
        sig = inspect.signature(func)

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            bound = sig.bind_partial(*args, **kwargs)
            arguments = dict(bound.arguments)
            outcome, error, result = "success", None, None
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as exc:
                outcome, error = "failure", {"type": exc.__class__.__name__, "code": getattr(exc, "code", None)}
                raise
            finally:
                try:
                    from ordering.repositories.audit import record_audit_event

                    resource_id = None
                    if resource is not None:
                        try:
                            resource_id = resource(arguments, result)
                        except Exception:
                            resource_id = None
                    await record_audit_event(
                        action=action,
                        outcome=outcome,
                        resource_id=resource_id,
                        details={"input": _summarize(arguments), "error": error},
                    )
                except Exception as audit_exc:  # pragma: no cover - defensive
                    log.warning("audit_write_failed", action=action, error=str(audit_exc))

        return wrapper

    return decorator
