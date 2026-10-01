"""Create/upgrade the LangGraph checkpoint tables with the owner role (after ``alembic upgrade``).

Run by the Kubernetes db-migrate job so agent-service can use ``CHECKPOINTER_AUTO_SETUP=false``
and hold only the least-privilege ``app_user`` credentials. Default privileges from migration
0005 grant ``app_user`` access to the tables created here.

    python -m scripts.setup_checkpointer
"""

from __future__ import annotations

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from ordering.common.config import get_settings
from ordering.common.db import psycopg_dsn


async def main() -> None:
    dsn = psycopg_dsn(get_settings().require_secret("database_admin_url"))
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    print("langgraph checkpointer tables ready")


if __name__ == "__main__":
    asyncio.run(main())
