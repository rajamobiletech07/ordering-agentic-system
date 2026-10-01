"""Shared test configuration.

Unit tests need no infrastructure. Integration tests need the compose Postgres + Redis
(``docker compose up -d postgres redis`` + migrate/seed); e2e tests need the full stack.
Both are skipped automatically when their dependencies are unreachable.
"""

from __future__ import annotations

import os
import socket

import pytest

os.environ.setdefault("ENV", "test")
os.environ.setdefault("LLM_PROVIDER_OVERRIDE", "mock")
os.environ.setdefault("EMBEDDING_PROFILE", "hashing")
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    infra = port_open("localhost", 55432) and port_open("localhost", 6379)
    stack = port_open("localhost", 8000)
    for item in items:
        if "integration" in item.keywords and not infra:
            item.add_marker(pytest.mark.skip(reason="Postgres/Redis not reachable (docker compose up -d postgres redis)"))
        if "e2e" in item.keywords and not stack:
            item.add_marker(pytest.mark.skip(reason="API gateway not reachable on :8000 (docker compose up -d)"))


@pytest.fixture
def fake_redis():
    import fakeredis

    from ordering.common import redis as redis_mod

    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    redis_mod.set_redis(client)
    yield client
    redis_mod.set_redis(None)
