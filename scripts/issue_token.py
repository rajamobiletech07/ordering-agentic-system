"""Issue a local development JWT for a seeded user (never for production).

    python -m scripts.issue_token u-acme-buyer
    export TOKEN=$(python -m scripts.issue_token u-acme-buyer --raw)
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from ordering.common.db import admin_session, dispose_engines
from ordering.security.jwt import issue_dev_token


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="seeded user, e.g. u-acme-buyer, u-acme-approver, u-globex-buyer")
    parser.add_argument("--ttl", type=int, default=3600)
    parser.add_argument("--raw", action="store_true", help="print only the token")
    args = parser.parse_args()
    try:
        async with admin_session() as s:
            row = (await s.execute(text("SELECT tenant_id, email, roles FROM users WHERE user_id = :u"),
                                   {"u": args.user_id})).first()
    finally:
        await dispose_engines()
    if row is None:
        raise SystemExit(f"unknown user {args.user_id}; run python -m scripts.seed first")
    token = issue_dev_token(args.user_id, row.tenant_id, list(row.roles), row.email, args.ttl)
    print(token if args.raw else json.dumps({"access_token": token, "tenant_id": row.tenant_id, "roles": list(row.roles)}))


if __name__ == "__main__":
    asyncio.run(main())
