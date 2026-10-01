"""Seed idempotency, RLS isolation, idempotency store, long-term memory."""

import asyncio
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

from ordering.common.context import Principal
from ordering.common.db import admin_session, tenant_session
from ordering.common.errors import Conflict, IdempotencyConflict
from ordering.memory import long_term
from ordering.repositories import idempotency

pytestmark = pytest.mark.integration
TABLES = ["recommendations", "orders", "payments", "notifications", "order_lines", "products", "vendors", "locations"]


async def _counts():
    async with admin_session() as s:
        return {t: (await s.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id IN ('acme-retail','globex-grocers') "
                                         f"AND ({'idempotency_key' if t in ('orders','payments','notifications') else 'true'} "
                                         f"{'LIKE' if t in ('orders','payments','notifications') else '='} "
                                         f"{chr(39)+'seed-%'+chr(39) if t in ('orders','payments','notifications') else 'true'})"))).scalar_one()
                for t in TABLES}


async def test_seed_is_repeatable_and_sized():
    before = await _counts()
    await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "scripts.seed"], check=True, capture_output=True)
    after = await _counts()
    assert before == after
    assert after["recommendations"] == 1000 and after["orders"] == 1000
    assert 850 <= after["payments"] <= 1100 and 900 <= after["notifications"] <= 1400


async def test_rls_isolates_tenants():
    async with tenant_session("globex-grocers") as s:
        tenants = {r[0] for r in await s.execute(text("SELECT DISTINCT tenant_id FROM orders"))}
        assert tenants == {"globex-grocers"}
        acme_rows = (await s.execute(text("SELECT count(*) FROM recommendations WHERE tenant_id = 'acme-retail'"))).scalar_one()
        assert acme_rows == 0
    with pytest.raises(Exception):  # WITH CHECK: cannot write rows for another tenant
        async with tenant_session("globex-grocers") as s:
            await s.execute(text("INSERT INTO conversations (conversation_id, tenant_id, user_id) VALUES (:c, 'acme-retail', 'x')"),
                            {"c": uuid.uuid4()})
    async with tenant_session("") as s:  # no tenant context → nothing visible
        assert (await s.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0


async def test_idempotency_store_semantics():
    key = f"k-{uuid.uuid4()}"
    assert await idempotency.begin("acme-retail", "test", key, "a" * 64) is None
    with pytest.raises(Conflict):
        await idempotency.begin("acme-retail", "test", key, "a" * 64)          # still in progress
    await idempotency.complete("acme-retail", "test", key, {"order": 1})
    assert await idempotency.begin("acme-retail", "test", key, "a" * 64) == {"order": 1, "replayed": True}
    with pytest.raises(IdempotencyConflict):
        await idempotency.begin("acme-retail", "test", key, "b" * 64)
    assert await idempotency.begin("globex-grocers", "test", key, "b" * 64) is None  # keys are tenant-scoped


async def test_long_term_memory_add_update_recall_erase():
    user = Principal(f"u-mem-{uuid.uuid4().hex[:6]}", "acme-retail", frozenset({"buyer"}))
    ops = long_term.rule_based_ops("I prefer paying with tok_mastercard", [])
    await long_term.apply_ops(user, ops, None)
    recalled = await long_term.recall(user, "how do I pay?")
    assert [m["content"] for m in recalled] == ["Prefers to pay with tok_mastercard"]
    update = long_term.rule_based_ops("I always pay with tok_amex", recalled)
    await long_term.apply_ops(user, update, None)
    assert [m["content"] for m in await long_term.list_memories(user)] == ["Prefers to pay with tok_amex"]
    blocked = long_term.MemoryOps(operations=[long_term.MemoryOp(event="ADD", content="my password is hunter2")])
    assert await long_term.apply_ops(user, blocked, None) == []                 # deny-listed content
    other = Principal(user.user_id, "globex-grocers", frozenset({"buyer"}))
    assert await long_term.list_memories(other) == []                          # tenant isolation
    assert await long_term.erase_all(user) == 1
    assert await long_term.list_memories(user) == []
