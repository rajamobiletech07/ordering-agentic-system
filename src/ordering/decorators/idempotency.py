"""Idempotency for mutation operations (DB-backed, tenant-scoped).

Protocol for ``(tenant, scope, key)``:

1. First call: a row is inserted in ``idempotency_keys`` with status ``in_progress`` (the
   unique constraint makes concurrent duplicates lose the race), the operation runs, and its
   JSON result is stored with status ``completed``.
2. Replay with the same request hash: the stored result is returned without re-executing.
3. Replay with a *different* request hash: :class:`IdempotencyConflict` (HTTP 409).
4. Replay while still ``in_progress``: :class:`Conflict` (client should retry later).
5. If the operation raises, the row is removed so the client may retry.

Business tables additionally carry ``UNIQUE (tenant_id, idempotency_key)`` as a second guard.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from ordering.common.context import require_principal
from ordering.common.ids import sha256_hex


def idempotent(scope: str, key_param: str = "idempotency_key", hash_of: Callable[[dict[str, Any]], Any] | None = None):
    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("idempotent only supports async functions")
        sig = inspect.signature(func)

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            from ordering.repositories import idempotency as repo

            bound = sig.bind_partial(*args, **kwargs)
            arguments = dict(bound.arguments)
            key = _find_key(arguments, key_param)
            if not key:
                return await func(*args, **kwargs)
            principal = require_principal()
            payload = hash_of(arguments) if hash_of else {k: _dump(v) for k, v in arguments.items()}
            request_hash = sha256_hex(payload)

            replay = await repo.begin(principal.tenant_id, scope, key, request_hash)
            if replay is not None:
                return replay
            try:
                result = await func(*args, **kwargs)
            except BaseException:
                await repo.abandon(principal.tenant_id, scope, key)
                raise
            await repo.complete(principal.tenant_id, scope, key, _dump(result))
            return result

        return wrapper

    return decorator


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _find_key(arguments: dict[str, Any], key_param: str) -> str | None:
    if arguments.get(key_param):
        return str(arguments[key_param])
    for value in arguments.values():
        candidate = getattr(value, key_param, None)
        if candidate:
            return str(candidate)
        if isinstance(value, dict) and value.get(key_param):
            return str(value[key_param])
    return None
