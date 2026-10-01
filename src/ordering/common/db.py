"""Async SQLAlchemy engine/session management with tenant-scoped transactions.

Every business transaction runs inside :func:`tenant_session`, which sets the Postgres
``app.tenant_id`` setting *locally to the transaction*. Row-level-security policies
(migration ``0004_rls``) compare every row against it, so even a buggy query cannot read
or write another tenant's data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ordering.common.config import get_settings

_engines: dict[str, AsyncEngine] = {}
_makers: dict[str, async_sessionmaker[AsyncSession]] = {}


def get_engine(admin: bool = False) -> AsyncEngine:
    key = "admin" if admin else "app"
    if key not in _engines:
        settings = get_settings()
        url = settings.require_secret("database_admin_url" if admin else "database_url")
        _engines[key] = create_async_engine(
            url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_size,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"options": f"-c statement_timeout={settings.db_statement_timeout_ms}"},
        )
        _makers[key] = async_sessionmaker(_engines[key], expire_on_commit=False)
    return _engines[key]


def get_sessionmaker(admin: bool = False) -> async_sessionmaker[AsyncSession]:
    get_engine(admin)
    return _makers["admin" if admin else "app"]


@asynccontextmanager
async def tenant_session(tenant_id: str, user_id: str | None = None) -> AsyncIterator[AsyncSession]:
    """One transaction bound to a tenant. Commits on success, rolls back on error."""
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true), set_config('app.user_id', :u, true)"),
            {"t": tenant_id, "u": user_id or ""},
        )
        yield session


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """Owner-role transaction (bypasses RLS) - migrations, seeding, cross-tenant jobs only."""
    maker = get_sessionmaker(admin=True)
    async with maker() as session, session.begin():
        yield session


async def ping() -> bool:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def dispose_engines() -> None:
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()
    _makers.clear()


def psycopg_dsn(url: str) -> str:
    """Convert a SQLAlchemy URL to a libpq DSN (for LangGraph's psycopg checkpointer)."""
    return url.replace("postgresql+psycopg://", "postgresql://")
