"""Structured JSON logging with correlation IDs, trace IDs and redaction.

Adapted from the mcp-core ``mcp_tool_logger`` (``inventory_smart_server/decorator.py``), which
used a plain-text StreamHandler; here every line is a JSON object carrying request/tenant/
trace identifiers so the telemetry collector can index and correlate it.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

from ordering.common.context import get_context
from ordering.common.redaction import redact

_configured = False


def _add_context(_, __, event_dict):
    ctx = get_context()
    event_dict.setdefault("request_id", ctx.request_id)
    if ctx.tenant_id:
        event_dict.setdefault("tenant_id", ctx.tenant_id)
    if ctx.user_id:
        event_dict.setdefault("user_id", ctx.user_id)
    if ctx.conversation_id:
        event_dict.setdefault("conversation_id", ctx.conversation_id)
    if ctx.agent:
        event_dict.setdefault("agent", ctx.agent)
    try:
        from opentelemetry import trace

        span_ctx = trace.get_current_span().get_span_context()
        if span_ctx.is_valid:
            event_dict["trace_id"] = format(span_ctx.trace_id, "032x")
            event_dict["span_id"] = format(span_ctx.span_id, "016x")
    except Exception:  # noqa: S110  # nosec B110 - trace ids are best-effort in logs
        pass
    return event_dict


def _gcp_fields(_, __, event_dict):
    """Cloud Logging structured-log fields (GKE ingests stdout JSON): ``severity`` for log levels and
    ``logging.googleapis.com/trace`` so log lines link to their Cloud Trace span."""
    event_dict["severity"] = str(event_dict.get("level", "info")).upper()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project and "trace_id" in event_dict:
        event_dict["logging.googleapis.com/trace"] = f"projects/{project}/traces/{event_dict['trace_id']}"
        event_dict["logging.googleapis.com/spanId"] = event_dict.get("span_id")
    return event_dict


def _redact_processor(_, __, event_dict):
    return redact(event_dict)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "uvicorn.access", "mcp", "fastmcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_context,
            _gcp_fields,
            structlog.processors.dict_tracebacks,
            _redact_processor,
            lambda _, __, ed: {"service": service_name, **ed},
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
