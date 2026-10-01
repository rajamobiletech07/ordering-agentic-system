"""Short-lived internal service tokens (agent → MCP server).

The agent service never forwards the user's API token to MCP servers. Instead it mints a
token scoped to the MCP audience that carries the *verified* user context plus the acting
agent (RFC 8693 ``act`` claim). MCP servers accept only these tokens, so a tool call can only
happen through an agent, on behalf of a real user, within that user's tenant and roles.
"""

from __future__ import annotations

import time
import uuid

import jwt

from ordering.common.config import Settings, get_settings
from ordering.common.context import Principal
from ordering.common.errors import AuthenticationError

ISSUER = "ordering-agent-service"


def mint_service_token(principal: Principal, agent: str, conversation_id: str | None = None,
                       settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": settings.service_jwt_audience,
        "sub": principal.user_id,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "act": {"sub": agent},
        "cid": conversation_id,
        "iat": now,
        "exp": now + settings.service_token_ttl_s,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.require_secret("service_jwt_secret"), algorithm="HS256")


def verify_service_token(token: str, settings: Settings | None = None) -> Principal:
    settings = settings or get_settings()
    if not token:
        raise AuthenticationError("missing service token")
    if token.lower().startswith("bearer "):
        token = token[7:]
    try:
        claims = jwt.decode(
            token,
            settings.require_secret("service_jwt_secret"),
            algorithms=["HS256"],
            audience=settings.service_jwt_audience,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "tenant_id", "roles", "act"]},
            leeway=5,
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError(f"invalid service token: {exc.__class__.__name__}") from exc
    act = claims.get("act") or {}
    return Principal(
        user_id=str(claims["sub"]),
        tenant_id=str(claims["tenant_id"]),
        roles=frozenset(claims.get("roles") or []),
        actor=act.get("sub"),
        raw_token=token,
    )
