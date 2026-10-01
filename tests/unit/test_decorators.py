import asyncio
import inspect
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ordering.common.context import Principal, RequestContext, reset_context, set_context
from ordering.common.errors import (
    CircuitOpen,
    DependencyTimeout,
    DependencyUnavailable,
    NotFound,
    PermissionDenied,
)
from ordering.decorators.auth import require_permissions
from ordering.decorators.errors import handle_errors
from ordering.decorators.observability import timed, traced
from ordering.decorators.resilience import CircuitBreaker, circuit, with_retry, with_timeout


class Item(BaseModel):
    name: str
    qty: int


@traced("test.span")
@timed()
async def typed_handler(item: Item, limit: int = 5) -> dict:
    return {"name": item.name, "qty": item.qty, "limit": limit}


def test_decorators_preserve_signature_for_frameworks():
    sig = inspect.signature(typed_handler)
    assert list(sig.parameters) == ["item", "limit"]
    assert sig.parameters["item"].annotation in (Item, "Item")
    app = FastAPI()
    app.post("/items")(typed_handler)
    resp = TestClient(app).post("/items?limit=3", json={"name": "milk", "qty": 2})
    assert resp.status_code == 200 and resp.json() == {"name": "milk", "qty": 2, "limit": 3}
    assert TestClient(app).post("/items", json={"name": "milk"}).status_code == 422


def test_decorators_reject_sync_functions():
    with pytest.raises(TypeError):
        traced()(lambda: None)
    with pytest.raises(TypeError):
        with_retry()(lambda: None)


async def test_retry_retries_transient_reads():
    calls = {"n": 0}

    @with_retry(3, base_delay=0.001)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise DependencyUnavailable("down")
        return "ok"

    assert await flaky() == "ok" and calls["n"] == 3


async def test_retry_never_repeats_mutation_without_idempotency_key():
    calls = {"n": 0}

    @with_retry(3, base_delay=0.001, mutation=True)
    async def create(arguments: dict):
        calls["n"] += 1
        raise DependencyUnavailable("down")

    with pytest.raises(DependencyUnavailable):
        await create({"qty": 1})
    assert calls["n"] == 1
    calls["n"] = 0
    with pytest.raises(DependencyUnavailable):
        await create({"qty": 1, "idempotency_key": "key-12345678"})
    assert calls["n"] == 3


async def test_retry_does_not_retry_business_errors():
    calls = {"n": 0}

    @with_retry(3, base_delay=0.001)
    async def missing():
        calls["n"] += 1
        raise NotFound("nope")

    with pytest.raises(NotFound):
        await missing()
    assert calls["n"] == 1


async def test_timeout_maps_to_dependency_timeout():
    @with_timeout(0.05)
    async def slow():
        await asyncio.sleep(1)

    with pytest.raises(DependencyTimeout):
        await slow()


async def test_circuit_breaker_opens_and_half_opens():
    breaker = CircuitBreaker("t", failure_threshold=2, reset_timeout_s=0.1)

    @circuit(breaker)
    async def call(fail: bool):
        if fail:
            raise DependencyUnavailable("down")
        return "ok"

    for _ in range(2):
        with pytest.raises(DependencyUnavailable):
            await call(True)
    assert breaker.state == "open"
    with pytest.raises(CircuitOpen):
        await call(False)
    await asyncio.sleep(0.12)
    assert breaker.state == "half_open"
    assert await call(False) == "ok"
    assert breaker.state == "closed"


async def test_handle_errors_emits_structured_tool_errors():
    from fastmcp.exceptions import ToolError

    @handle_errors("mcp")
    async def tool():
        raise NotFound("order not found", details={"order_id": "x"})

    with pytest.raises(ToolError) as exc:
        await tool()
    payload = json.loads(str(exc.value))
    assert payload["code"] == "not_found" and payload["details"] == {"order_id": "x"}

    @handle_errors("mcp")
    async def boom():
        raise RuntimeError("secret internal detail")

    with pytest.raises(ToolError) as exc:
        await boom()
    assert "secret" not in str(exc.value) and json.loads(str(exc.value))["code"] == "internal_error"


async def test_require_permissions_uses_verified_principal():
    @require_permissions("orders:create")
    async def create():
        return "created"

    token = set_context(RequestContext(principal=Principal("u", "acme-retail", frozenset({"viewer"}))))
    try:
        with pytest.raises(PermissionDenied):
            await create()
    finally:
        reset_context(token)
    token = set_context(RequestContext(principal=Principal("u", "acme-retail", frozenset({"buyer"}))))
    try:
        assert await create() == "created"
    finally:
        reset_context(token)
