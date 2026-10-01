"""Deterministic identifiers and hashing helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

NAMESPACE = uuid.UUID("5b0c4c2e-8f0e-4f7b-9a57-2f7d8c1e0a11")


def stable_uuid(*parts: Any) -> uuid.UUID:
    """uuid5 over the joined parts - used for reproducible seed data and dedup keys."""
    return uuid.uuid5(NAMESPACE, "|".join(str(p) for p in parts))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(value: Any) -> str:
    data = value if isinstance(value, (bytes, bytearray)) else (value if isinstance(value, str) else canonical_json(value)).encode()
    return hashlib.sha256(data).hexdigest()


def new_id() -> uuid.UUID:
    return uuid.uuid4()
