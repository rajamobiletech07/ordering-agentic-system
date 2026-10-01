import time

import jwt
import pytest

from ordering.common.config import get_settings
from ordering.common.context import Principal
from ordering.common.errors import AuthenticationError, ToolAccessDenied
from ordering.security.jwt import issue_dev_token, verify_user_token
from ordering.security.permissions import (
    ORDERING_AGENT,
    RECOMMENDATION_AGENT,
    TOOL_POLICIES,
    authorize_tool,
    has_permissions,
    tools_for_agent,
)
from ordering.security.service_tokens import mint_service_token, verify_service_token


def test_user_token_roundtrip():
    token = issue_dev_token("u-acme-buyer", "acme-retail", ["buyer"], "p@acme.example")
    p = verify_user_token(token)
    assert (p.user_id, p.tenant_id, p.roles) == ("u-acme-buyer", "acme-retail", frozenset({"buyer"}))


@pytest.mark.parametrize("mutate", ["expired", "audience", "signature", "missing_tenant", "service_token"])
def test_user_token_rejections(mutate):
    s = get_settings()
    now = int(time.time())
    claims = {"sub": "u", "tenant_id": "acme-retail", "roles": ["buyer"], "iss": s.jwt_issuer, "aud": s.jwt_audience,
              "iat": now, "exp": now + 60}
    key = s.secret("jwt_secret")
    if mutate == "expired":
        claims["exp"] = now - 120
    elif mutate == "audience":
        claims["aud"] = "someone-else"
    elif mutate == "signature":
        key = "not-the-secret-not-the-secret-not-the-secret"
    elif mutate == "missing_tenant":
        claims.pop("tenant_id")
    token = jwt.encode(claims, key, algorithm="HS256")
    if mutate == "service_token":  # a service token must never be accepted at the public edge
        token = mint_service_token(Principal("u", "acme-retail", frozenset({"buyer"})), ORDERING_AGENT)
    with pytest.raises(AuthenticationError):
        verify_user_token(token)


def test_unknown_roles_are_dropped():
    s = get_settings()
    now = int(time.time())
    token = jwt.encode({"sub": "u", "tenant_id": "t-acme", "roles": ["buyer", "superuser"], "iss": s.jwt_issuer,
                        "aud": s.jwt_audience, "iat": now, "exp": now + 60}, s.secret("jwt_secret"), algorithm="HS256")
    assert verify_user_token(token).roles == frozenset({"buyer"})


def test_service_token_carries_actor():
    p = Principal("u-1", "acme-retail", frozenset({"buyer"}))
    verified = verify_service_token("Bearer " + mint_service_token(p, RECOMMENDATION_AGENT, "c-1"))
    assert verified.actor == RECOMMENDATION_AGENT and verified.tenant_id == "acme-retail"


def test_role_hierarchy():
    assert has_permissions({"viewer"}, "orders:read")
    assert not has_permissions({"viewer"}, "orders:create")
    assert has_permissions({"buyer"}, "orders:create", "payments:initiate")
    assert not has_permissions({"buyer"}, "orders:approve_high_value")
    assert has_permissions({"admin"}, "audit:read", "rag:ingest")


def test_agent_tool_allowlist_enforced_outside_llm():
    buyer = frozenset({"buyer"})
    authorize_tool("ordering.create_order", ORDERING_AGENT, buyer)
    with pytest.raises(ToolAccessDenied):  # recommendation agent can never create orders
        authorize_tool("ordering.create_order", RECOMMENDATION_AGENT, buyer)
    with pytest.raises(ToolAccessDenied):  # viewer can't, even through the right agent
        authorize_tool("ordering.create_order", ORDERING_AGENT, frozenset({"viewer"}))
    with pytest.raises(ToolAccessDenied):
        authorize_tool("payment.process_payment", "supervisor", buyer)
    with pytest.raises(ToolAccessDenied):
        authorize_tool("ordering.drop_tables", ORDERING_AGENT, frozenset({"admin"}))


def test_every_mutation_requires_approval_except_notifications():
    for policy in TOOL_POLICIES.values():
        if policy.mutation and policy.server != "notification":
            assert policy.requires_approval, policy.qualified_name
    assert all(not p.mutation for p in tools_for_agent(RECOMMENDATION_AGENT))
