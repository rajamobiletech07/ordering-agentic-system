"""Default privileges + LangGraph checkpoint tables.

Tables created later by the owner role (e.g. LangGraph's checkpoint tables, created by
``AsyncPostgresSaver.setup()`` on agent-service startup with the owner DSN) automatically grant
DML to ``app_user``. Checkpoint rows carry no tenant column: the agent service only opens a
thread after verifying the conversation belongs to the caller's tenant/user (see ARCHITECTURE.md).

Revision ID: 0005_default_privileges
Revises: 0004_rls
Create Date: 2026-09-30
"""

from alembic import op

revision = "0005_default_privileges"
down_revision = "0004_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_user;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM app_user;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM app_user;
        """
    )
