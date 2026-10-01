"""Per-request context propagated through async call chains via contextvars.

Replaces the global mutable ``HEADERS`` dict used in mcp-core ``inventory_smart_server/utils.py``,
which leaked headers between concurrent requests.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Principal:
    """A verified identity. Only ever constructed from a validated token."""

    user_id: str
    tenant_id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    email: str | None = None
    # Set on service tokens: the agent acting on behalf of the user (RFC 8693 ``act``).
    actor: str | None = None
    raw_token: str | None = field(default=None, repr=False, compare=False)

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


@dataclass(frozen=True)
class RequestContext:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    principal: Principal | None = None
    conversation_id: str | None = None
    agent: str | None = None
    prompt_version: str | None = None

    @property
    def tenant_id(self) -> str | None:
        return self.principal.tenant_id if self.principal else None

    @property
    def user_id(self) -> str | None:
        return self.principal.user_id if self.principal else None


_ctx: ContextVar[RequestContext] = ContextVar("request_context", default=RequestContext())  # noqa: B039


def get_context() -> RequestContext:
    return _ctx.get()


def set_context(ctx: RequestContext):
    return _ctx.set(ctx)


def reset_context(token) -> None:
    _ctx.reset(token)


def update_context(**changes) -> RequestContext:
    ctx = replace(_ctx.get(), **changes)
    _ctx.set(ctx)
    return ctx


def require_principal() -> Principal:
    principal = _ctx.get().principal
    if principal is None:
        from ordering.common.errors import AuthenticationError

        raise AuthenticationError("no authenticated principal in context")
    return principal
