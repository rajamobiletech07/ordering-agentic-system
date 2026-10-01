import asyncio
import socket

import pytest

from ordering.common.context import Principal, RequestContext, set_context


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
async def mcp_urls():
    """Run the four MCP servers in-process (real streamable-HTTP transport) on random ports."""
    import uvicorn

    from ordering.mcp_servers.common import build_app
    from ordering.mcp_servers.notification import server as notification
    from ordering.mcp_servers.ordering import server as ordering
    from ordering.mcp_servers.payment import server as payment
    from ordering.mcp_servers.recommendation import server as recommendation

    servers, urls = [], {}
    for name, module in [("recommendation", recommendation), ("ordering", ordering), ("payment", payment),
                         ("notification", notification)]:
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(build_app(module.mcp), host="127.0.0.1", port=port, log_level="warning"))
        servers.append((server, asyncio.create_task(server.serve())))
        urls[name] = f"http://127.0.0.1:{port}/mcp"
    for server, _ in servers:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
    yield urls
    for server, task in servers:
        server.should_exit = True
        await task


@pytest.fixture
def mcp(mcp_urls):
    from ordering.agents.mcp_client import MCPToolClient

    client = MCPToolClient()
    client.urls = dict(mcp_urls)
    return client


@pytest.fixture
def as_user():
    def _set(user_id="u-acme-buyer", tenant="acme-retail", roles=("buyer",), agent="ordering_agent"):
        set_context(RequestContext(principal=Principal(user_id, tenant, frozenset(roles)), agent=agent))
        return Principal(user_id, tenant, frozenset(roles))

    yield _set
    set_context(RequestContext())  # teardown may run in another context: overwrite, don't reset
