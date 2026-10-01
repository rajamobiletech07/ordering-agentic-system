"""Domain error hierarchy with stable, machine-readable error codes.

Every error maps to an HTTP status (API boundary, RFC 9457 problem+json) and to an MCP
``ToolError`` payload (tool boundary), so clients and agents can branch on ``code``
instead of parsing messages.
"""

from __future__ import annotations

from typing import Any


class OrderingError(Exception):
    code: str = "internal_error"
    status_code: int = 500
    retryable: bool = False

    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None, code: str | None = None):
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.code.replace("_", " ")
        self.details = details or {}
        if code:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


class ValidationFailed(OrderingError):
    code, status_code = "validation_failed", 422


class AuthenticationError(OrderingError):
    code, status_code = "unauthenticated", 401


class PermissionDenied(OrderingError):
    code, status_code = "permission_denied", 403


class ToolAccessDenied(PermissionDenied):
    code = "tool_access_denied"


class NotFound(OrderingError):
    code, status_code = "not_found", 404


class Conflict(OrderingError):
    code, status_code = "conflict", 409


class IdempotencyConflict(Conflict):
    code = "idempotency_key_reused"


class ApprovalRequired(OrderingError):
    code, status_code = "approval_required", 409


class InvalidState(Conflict):
    code = "invalid_state"


class RateLimited(OrderingError):
    code, status_code, retryable = "rate_limited", 429, True


class BudgetExceeded(OrderingError):
    code, status_code = "budget_exceeded", 429


class DependencyUnavailable(OrderingError):
    code, status_code, retryable = "dependency_unavailable", 503, True


class DependencyTimeout(DependencyUnavailable):
    code = "dependency_timeout"


class CircuitOpen(DependencyUnavailable):
    code = "circuit_open"


class PaymentDeclined(OrderingError):
    code, status_code = "payment_declined", 402


class GuardrailViolation(OrderingError):
    code, status_code = "guardrail_violation", 400


class LLMUnavailable(DependencyUnavailable):
    code = "llm_unavailable"


_BY_CODE: dict[str, type[OrderingError]] = {}


def _register(cls: type[OrderingError]) -> None:
    _BY_CODE[cls.code] = cls
    for sub in cls.__subclasses__():
        _register(sub)


_register(OrderingError)


def error_from_dict(payload: dict[str, Any]) -> OrderingError:
    """Rehydrate an error that crossed a service boundary."""
    cls = _BY_CODE.get(payload.get("code", ""), OrderingError)
    err = cls(payload.get("message", ""), details=payload.get("details"))
    err.code = payload.get("code", err.code)
    return err
