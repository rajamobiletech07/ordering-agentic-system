"""User token verification (and dev-only issuance).

Adapted from mcp-core ``auth_decorator.verify_token``, which made a network call to a
backend on *every* tool call. Here tokens are verified locally (signature, ``exp``, ``nbf``,
``iss``, ``aud``, required claims - RFC 8725 best practices). With ``JWT_JWKS_URL`` set,
RS256/ES256 keys are fetched from the IdP's JWKS endpoint and cached.
"""

from __future__ import annotations

import time
import uuid
from functools import lru_cache

import jwt

from ordering.common.config import Settings, get_settings
from ordering.common.context import Principal
from ordering.common.errors import AuthenticationError

_REQUIRED = ["exp", "iat", "sub", "tenant_id", "roles", "iss", "aud"]
VALID_ROLES = {"viewer", "buyer", "approver", "admin"}


@lru_cache
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300)


def _principal_from_claims(claims: dict, token: str) -> Principal:
    roles = claims.get("roles") or []
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise AuthenticationError("invalid roles claim")
    tenant = claims.get("tenant_id")
    if not isinstance(tenant, str) or not tenant:
        raise AuthenticationError("invalid tenant claim")
    return Principal(
        user_id=str(claims["sub"]),
        tenant_id=tenant,
        roles=frozenset(r for r in roles if r in VALID_ROLES),
        email=claims.get("email"),
        actor=(claims.get("act") or {}).get("sub") if isinstance(claims.get("act"), dict) else None,
        raw_token=token,
    )


def verify_user_token(token: str, settings: Settings | None = None) -> Principal:
    settings = settings or get_settings()
    if not token:
        raise AuthenticationError("missing bearer token")
    try:
        if settings.jwt_jwks_url:
            key = _jwks_client(settings.jwt_jwks_url).get_signing_key_from_jwt(token).key
            algorithms = ["RS256", "ES256"]
        else:
            key = settings.require_secret("jwt_secret")
            algorithms = [settings.jwt_algorithm]
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": _REQUIRED},
            leeway=10,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError(f"invalid token: {exc.__class__.__name__}") from exc
    return _principal_from_claims(claims, token)


def issue_dev_token(
    user_id: str,
    tenant_id: str,
    roles: list[str],
    email: str | None = None,
    ttl_s: int = 3600,
    settings: Settings | None = None,
) -> str:
    """HS256 token for local development / tests. Disabled in production by config guard."""
    settings = settings or get_settings()
    unknown = set(roles) - VALID_ROLES
    if unknown:
        raise ValueError(f"unknown roles: {unknown}")
    now = int(time.time())
    claims = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "roles": roles,
        "email": email,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_s,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.require_secret("jwt_secret"), algorithm="HS256")
