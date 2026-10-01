"""Idempotent database bootstrap, run before ``alembic upgrade head`` on managed Postgres.

Locally ``docker/postgres/init.sh`` does this on first start. On Cloud SQL there is no init hook,
and users created through the Cloud SQL API join ``cloudsqlsuperuser``, which is too privileged
for a role that RLS must apply to. So the owner role (``DATABASE_ADMIN_URL``) creates
``app_user`` in SQL: plain LOGIN, NOSUPERUSER and NOBYPASSRLS, and not a member of any
privileged role. The password comes from ``APP_DB_PASSWORD``, or from ``APP_DB_PASSWORD_FILE``
when it is mounted from Secret Manager. Rerunning the script rotates the password to the current
secret value.

    python -m scripts.db_bootstrap
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from ordering.common.config import get_settings
from ordering.common.db import psycopg_dsn

APP_ROLE = "app_user"


def _app_password() -> str:
    path = os.environ.get("APP_DB_PASSWORD_FILE")
    value = Path(path).read_text(encoding="utf-8").strip() if path else os.environ.get("APP_DB_PASSWORD", "")
    if not value:
        sys.exit("APP_DB_PASSWORD or APP_DB_PASSWORD_FILE must be set")
    if get_settings().env in ("staging", "production") and len(value) < 20:
        sys.exit("refusing a weak app_user password outside local/test")
    return value


def main() -> None:
    password = _app_password()
    dsn = psycopg_dsn(get_settings().require_secret("database_admin_url"))
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        for ext in ("vector", "pgcrypto"):
            conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
            sql.Identifier(APP_ROLE), sql.Literal(password)))
        row = conn.execute(
            """SELECT r.rolsuper, r.rolbypassrls,
                      ARRAY(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                            WHERE m.member = r.oid)
               FROM pg_roles r WHERE r.rolname = %s""", (APP_ROLE,)).fetchone()
        assert row is not None
        superuser, bypassrls, member_of = row
        if superuser or bypassrls or "cloudsqlsuperuser" in member_of:
            sys.exit(f"{APP_ROLE} is privileged (super={superuser}, bypassrls={bypassrls}, member_of={member_of}); "
                     "RLS would not apply - recreate it without those attributes")
    print(f"db bootstrap ok: extensions ready, {APP_ROLE} {'created' if verb == 'CREATE' else 'updated'} (LOGIN, no RLS bypass)")


if __name__ == "__main__":
    main()
