"""Notification MCP server: exposes the notification service to agents.

``send_notification`` only *enqueues* (durable, idempotent). Delivery happens asynchronously
in the notification worker (retries with exponential backoff, dead-lettering), which records
delivery status that ``get_notification_status`` returns. Nothing is sent externally.
"""

from __future__ import annotations

from uuid import UUID

from ordering.common.config import get_settings
from ordering.common.context import require_principal
from ordering.common.db import tenant_session
from ordering.common.errors import ValidationFailed
from ordering.decorators.idempotency import idempotent
from ordering.decorators.observability import audited
from ordering.decorators.tool_access import agent_tool
from ordering.domain.schemas.notification import SendNotification
from ordering.mcp_servers.common import create_mcp, serve
from ordering.repositories import notifications as repo

mcp = create_mcp("notification-mcp", "Queue simulated notifications (email/sms/webhook/in-app) and read delivery status.")


@mcp.tool(name="send_notification", annotations={"readOnlyHint": False, "idempotentHint": True, "openWorldHint": False})
@agent_tool("notification", "send_notification")
@audited("notification.enqueue", resource=lambda a, r: str(r.get("notification_id")) if isinstance(r, dict) else None)
@idempotent("notification.send", hash_of=lambda a: a["request"].model_dump(mode="json"))
async def send_notification(request: SendNotification) -> dict:
    """Queue a notification for asynchronous (simulated) delivery. Returns status ``queued``."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        existing = await repo.find_by_idempotency_key(s, request.idempotency_key)
        if existing is not None:
            existing.replayed = True
            return existing.model_dump(mode="json")
        recipient = request.recipient
        if recipient is None:
            if request.channel == "email":
                recipient = await repo.user_email(s, p.user_id)
            elif request.channel == "in_app":
                recipient = p.user_id
        if not recipient:
            raise ValidationFailed("recipient is required for this channel")
        notification = await repo.enqueue(
            s, tenant_id=p.tenant_id, order_id=request.order_id, channel=request.channel, recipient=recipient,
            template=request.template, payload=request.data, idempotency_key=request.idempotency_key,
            max_attempts=get_settings().notification_max_attempts,
        )
    return notification.model_dump(mode="json")


@mcp.tool(name="get_notification_status", annotations={"readOnlyHint": True, "idempotentHint": True})
@agent_tool("notification", "get_notification_status")
async def get_notification_status(notification_id: UUID) -> dict:
    """Delivery status, attempts and last error for a notification."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        return (await repo.get(s, notification_id)).model_dump(mode="json")


@mcp.tool(name="list_notifications", annotations={"readOnlyHint": True, "idempotentHint": True})
@agent_tool("notification", "list_notifications")
async def list_notifications(order_id: UUID) -> dict:
    """All notifications recorded for an order."""
    p = require_principal()
    async with tenant_session(p.tenant_id, p.user_id) as s:
        items = await repo.list_for_order(s, order_id)
    return {"notifications": [n.model_dump(mode="json") for n in items], "count": len(items)}


def main() -> None:
    serve(mcp, "mcp-notification", 9004)


if __name__ == "__main__":
    main()
