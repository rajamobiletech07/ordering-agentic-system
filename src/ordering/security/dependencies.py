"""FastAPI dependencies for authentication and permission checks."""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ordering.common.context import Principal, update_context
from ordering.common.errors import AuthenticationError, PermissionDenied
from ordering.security.jwt import verify_user_token
from ordering.security.permissions import has_permissions

_bearer = HTTPBearer(auto_error=False, description="OAuth2 bearer JWT (RFC 6750)")


async def get_principal(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("missing bearer token")
    principal = verify_user_token(credentials.credentials)
    request.state.principal = principal
    update_context(principal=principal)
    return principal


def require_permissions(*permissions: str):
    """Dependency factory: ``Depends(require_permissions(ORD_READ))``."""

    async def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not has_permissions(principal.roles, *permissions):
            raise PermissionDenied(
                "insufficient permissions", details={"required": list(permissions), "roles": sorted(principal.roles)}
            )
        return principal

    return _dep
