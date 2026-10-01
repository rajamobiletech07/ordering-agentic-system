"""Authentication / permission decorators for in-process (non-FastAPI) async functions.

For HTTP routes use the FastAPI dependencies in :mod:`ordering.security.dependencies`
(they integrate with OpenAPI). For MCP tools use :func:`ordering.decorators.tool_access.agent_tool`.
This decorator covers service-layer functions (agents, repositories, CLI jobs) so a
permission check travels with the function wherever it is called from.
"""

from __future__ import annotations

import functools
import inspect

from ordering.common.context import require_principal
from ordering.common.errors import PermissionDenied
from ordering.security.permissions import has_permissions


def require_permissions(*permissions: str, roles_any: tuple[str, ...] = ()):
    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("require_permissions only supports async functions")

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            principal = require_principal()
            if permissions and not has_permissions(principal.roles, *permissions):
                raise PermissionDenied("insufficient permissions", details={"required": list(permissions)})
            if roles_any and not principal.has_role(*roles_any):
                raise PermissionDenied("role required", details={"any_of": list(roles_any)})
            return await func(*args, **kwargs)

        return wrapper

    return decorator
