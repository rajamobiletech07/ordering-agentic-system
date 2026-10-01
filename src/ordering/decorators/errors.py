"""Consistent exception handling at service boundaries."""

from __future__ import annotations

import functools
import inspect
import json

from pydantic import ValidationError

from ordering.common.errors import OrderingError, ValidationFailed
from ordering.common.logging import get_logger

log = get_logger("ordering.errors")


def to_ordering_error(exc: Exception) -> OrderingError:
    if isinstance(exc, OrderingError):
        return exc
    if isinstance(exc, ValidationError):
        return ValidationFailed("invalid input", details={"errors": exc.errors(include_url=False, include_input=False)})
    if isinstance(exc, TimeoutError):
        from ordering.common.errors import DependencyTimeout

        return DependencyTimeout("operation timed out")
    return OrderingError("internal error")


def handle_errors(boundary: str = "mcp"):
    """Map exceptions to the boundary's error contract.

    * ``mcp``: raise ``fastmcp.exceptions.ToolError`` whose message is a JSON error object
      (``code``/``message``/``retryable``/``details``) so the agent can branch on ``code``.
    * ``internal``: normalize to :class:`OrderingError` (for in-process callers).

    Unexpected exceptions are logged with stack trace; internals are never leaked to callers.
    """

    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("handle_errors only supports async functions")

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                err = to_ordering_error(exc)
                if not isinstance(exc, (OrderingError, ValidationError)):
                    log.exception("unhandled_error", fn=func.__qualname__)
                else:
                    log.info("handled_error", fn=func.__qualname__, code=err.code)
                if boundary == "mcp":
                    from fastmcp.exceptions import ToolError

                    raise ToolError(json.dumps(err.to_dict(), default=str)) from exc
                raise err from exc

        return wrapper

    return decorator
