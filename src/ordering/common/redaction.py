"""Sensitive-data redaction for logs, audit payloads, traces and long-term memory."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "authorization",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "card_number",
    "cvv",
    "payment_method_token",
    "cookie",
    "set-cookie",
    "x-api-key",
}

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(r"(?<!\d)\+?\d{1,3}[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact_text(text: str, classes: set[str] | None = None) -> str:
    """Mask sensitive substrings. ``classes`` limits which pattern classes apply."""
    if not text:
        return text
    for name, pattern in _PATTERNS:
        if classes is not None and name not in classes:
            continue
        if name == "card":
            def _card(m: re.Match[str]) -> str:
                digits = re.sub(r"\D", "", m.group(0))
                return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)

            text = pattern.sub(_card, text)
        else:
            text = pattern.sub(f"[{name.upper()}]", text)
    return text


def redact(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact dicts/lists/strings. Keys in ``_SENSITIVE_KEYS`` are fully masked."""
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, depth=depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
