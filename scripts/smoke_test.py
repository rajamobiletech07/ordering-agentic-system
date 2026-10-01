"""Post-deploy smoke test, run in-cluster as a Kubernetes Job after each rollout.

* ``--mode health`` (every environment): api-gateway ``/healthz`` and ``/readyz``, where readyz
  covers agent-service, the MCP servers, Postgres and Redis.
* ``--mode workflow`` (staging only, which holds seeded dummy data): mints a short-lived token
  for a seeded buyer and runs a read-only conversation turn, reads the conversation back, then closes it. It never
  places an order, so it is safe to run on every deploy.

    python -m scripts.smoke_test --base-url http://api-gateway --mode workflow
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx


def _check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        sys.exit(1)


def _wait_ready(client: httpx.Client, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            r = client.get("/readyz")
            if r.status_code == 200:
                _check(True, f"GET /readyz -> {r.json()}")
                return
            last = f"{r.status_code} {r.text[:200]}"
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(3)
    _check(False, f"GET /readyz never became ready: {last}")


def _mint_token(user_id: str) -> str:
    from sqlalchemy import text

    from ordering.common.db import admin_session, dispose_engines
    from ordering.security.jwt import issue_dev_token

    async def _lookup():
        try:
            async with admin_session() as s:
                return (await s.execute(text("SELECT tenant_id, email, roles FROM users WHERE user_id = :u"),
                                        {"u": user_id})).first()
        finally:
            await dispose_engines()

    row = asyncio.run(_lookup())
    _check(row is not None, f"seeded user {user_id} exists")
    return issue_dev_token(user_id, row.tenant_id, list(row.roles), row.email, 300)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://api-gateway")
    ap.add_argument("--mode", choices=["health", "workflow"], default="health")
    ap.add_argument("--user", default="u-acme-buyer")
    ap.add_argument("--ready-timeout", type=float, default=180)
    args = ap.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        r = client.get("/healthz")
        _check(r.status_code == 200, f"GET /healthz -> {r.status_code}")
        _wait_ready(client, args.ready_timeout)
        if args.mode == "health":
            return

        headers = {"Authorization": f"Bearer {_mint_token(args.user)}"}
        r = client.post("/v1/chat", json={"message": "What should I order for STORE-014?"}, headers=headers)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _check(r.status_code == 200 and bool(body.get("reply")) and bool(body.get("conversation_id")),
               f"POST /v1/chat (recommendations) -> {r.status_code}")
        cid = body["conversation_id"]
        r = client.get(f"/v1/conversations/{cid}", headers=headers)
        _check(r.status_code == 200, f"GET /v1/conversations/{{id}} (durable state) -> {r.status_code}")
        r = client.post(f"/v1/conversations/{cid}/close", headers=headers)
        _check(r.status_code in (200, 204), f"POST /v1/conversations/{{id}}/close (cache cleanup) -> {r.status_code}")


if __name__ == "__main__":
    main()
