"""Alembic environment. Always migrates with the *owner* role (DATABASE_ADMIN_URL)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from ordering.common.config import get_settings

config = context.config


def _url() -> str:
    return get_settings().secret("database_admin_url")


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
