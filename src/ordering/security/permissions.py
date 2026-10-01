"""Role → permission model and the agent → tool allowlist.

This is the single source of truth for authorization, enforced in code at the API boundary
(FastAPI dependencies), in the agent's MCP client, and again inside every MCP server - never
delegated to the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- permissions ---------------------------------------------------------------------------
REC_READ = "recommendations:read"
ORD_READ = "orders:read"
ORD_CREATE = "orders:create"
ORD_UPDATE = "orders:update"
ORD_CANCEL = "orders:cancel"
ORD_APPROVE_HIGH = "orders:approve_high_value"
PAY_INITIATE = "payments:initiate"
PAY_READ = "payments:read"
NOTIFY_SEND = "notifications:send"
NOTIFY_READ = "notifications:read"
POLICY_READ = "policies:read"
CHAT = "chat:use"
MEMORY_MANAGE = "memory:manage"
FEEDBACK_WRITE = "feedback:write"
RAG_INGEST = "rag:ingest"
AUDIT_READ = "audit:read"
CACHE_ADMIN = "cache:admin"

_VIEWER = {REC_READ, ORD_READ, PAY_READ, NOTIFY_READ, POLICY_READ, CHAT, MEMORY_MANAGE, FEEDBACK_WRITE}
_BUYER = _VIEWER | {ORD_CREATE, PAY_INITIATE, NOTIFY_SEND, ORD_CANCEL}
_APPROVER = _BUYER | {ORD_UPDATE, ORD_APPROVE_HIGH}
_ADMIN = _APPROVER | {RAG_INGEST, AUDIT_READ, CACHE_ADMIN}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset(_VIEWER),
    "buyer": frozenset(_BUYER),
    "approver": frozenset(_APPROVER),
    "admin": frozenset(_ADMIN),
}


def permissions_for(roles: frozenset[str] | set[str]) -> frozenset[str]:
    perms: set[str] = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(perms)


def has_permissions(roles: frozenset[str] | set[str], *required: str) -> bool:
    return set(required).issubset(permissions_for(roles))


# --- agents & tools ------------------------------------------------------------------------
RECOMMENDATION_AGENT = "recommendation_agent"
ORDERING_AGENT = "ordering_agent"
SUPERVISOR = "supervisor"
AGENTS = {RECOMMENDATION_AGENT, ORDERING_AGENT, SUPERVISOR}


@dataclass(frozen=True)
class ToolPolicy:
    server: str
    tool: str
    permission: str
    agents: frozenset[str]
    mutation: bool = False
    requires_approval: bool = False

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.tool}"


def _p(server, tool, permission, agents, mutation=False, approval=False) -> ToolPolicy:
    return ToolPolicy(server, tool, permission, frozenset(agents), mutation, approval)


_R, _O = RECOMMENDATION_AGENT, ORDERING_AGENT

TOOL_POLICIES: dict[str, ToolPolicy] = {
    p.qualified_name: p
    for p in [
        # Recommendation MCP server (read-only)
        _p("recommendation", "list_recommendations", REC_READ, {_R}),
        _p("recommendation", "get_recommendation", REC_READ, {_R, _O}),
        _p("recommendation", "get_inventory_position", REC_READ, {_R}),
        _p("recommendation", "list_inventory_risks", REC_READ, {_R}),
        _p("recommendation", "get_vendor", REC_READ, {_R, _O}),
        # Ordering MCP server
        _p("ordering", "list_orders", ORD_READ, {_R, _O}),
        _p("ordering", "get_order", ORD_READ, {_O}),
        _p("ordering", "create_order", ORD_CREATE, {_O}, mutation=True, approval=True),
        _p("ordering", "update_order", ORD_UPDATE, {_O}, mutation=True, approval=True),
        _p("ordering", "cancel_order", ORD_CANCEL, {_O}, mutation=True, approval=True),
        # Payment MCP server
        _p("payment", "process_payment", PAY_INITIATE, {_O}, mutation=True, approval=True),
        _p("payment", "get_payment_status", PAY_READ, {_O}),
        # Notification MCP server
        _p("notification", "send_notification", NOTIFY_SEND, {_O}, mutation=True),
        _p("notification", "get_notification_status", NOTIFY_READ, {_O}),
        _p("notification", "list_notifications", NOTIFY_READ, {_O}),
    ]
}


def tools_for_agent(agent: str) -> list[ToolPolicy]:
    return [p for p in TOOL_POLICIES.values() if agent in p.agents]


def authorize_tool(qualified_name: str, agent: str | None, roles: frozenset[str]) -> ToolPolicy:
    """Raise unless ``agent`` may call the tool *and* the user's roles grant its permission."""
    from ordering.common.errors import ToolAccessDenied

    policy = TOOL_POLICIES.get(qualified_name)
    if policy is None:
        raise ToolAccessDenied(f"unknown tool {qualified_name}")
    if agent not in policy.agents:
        raise ToolAccessDenied(
            f"agent '{agent}' is not allowed to call {qualified_name}",
            details={"tool": qualified_name, "agent": agent},
        )
    if not has_permissions(roles, policy.permission):
        raise ToolAccessDenied(
            f"missing permission {policy.permission} for {qualified_name}",
            details={"tool": qualified_name, "permission": policy.permission},
        )
    return policy
