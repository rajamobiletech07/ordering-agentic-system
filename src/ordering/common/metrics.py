"""Prometheus metrics shared by all services (scraped at ``/metrics``)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUESTS = Counter("ordering_http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
REQUEST_LATENCY = Histogram(
    "ordering_http_request_seconds", "HTTP latency", ["service", "route"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)
)
TOOL_CALLS = Counter("ordering_tool_calls_total", "MCP tool calls", ["server", "tool", "agent", "outcome"])
TOOL_LATENCY = Histogram("ordering_tool_call_seconds", "MCP tool latency", ["server", "tool"])
LLM_CALLS = Counter("ordering_llm_calls_total", "LLM provider attempts", ["route", "provider", "model", "outcome"])
LLM_FALLBACKS = Counter("ordering_llm_fallbacks_total", "LLM fallbacks to next provider", ["route", "from_provider"])
LLM_TOKENS = Counter("ordering_llm_tokens_total", "LLM tokens", ["provider", "model", "kind"])
LLM_COST = Counter("ordering_llm_cost_usd_total", "LLM cost in USD", ["tenant", "provider", "model"])
LLM_LATENCY = Histogram("ordering_llm_call_seconds", "LLM latency", ["provider", "model"])
CACHE_EVENTS = Counter("ordering_cache_events_total", "Thread cache events", ["kind", "event"])
AGENT_TURNS = Counter("ordering_agent_turns_total", "Supervisor turns", ["intent", "outcome"])
APPROVALS = Counter("ordering_approvals_total", "Approval decisions", ["action", "decision"])
GUARDRAIL_HITS = Counter("ordering_guardrail_hits_total", "Guardrail detections", ["guardrail", "action"])
NOTIFICATIONS = Counter("ordering_notifications_total", "Notification deliveries", ["channel", "status"])
FEEDBACK = Counter("ordering_feedback_total", "User feedback", ["rating"])
EVAL_SCORE = Histogram("ordering_eval_score", "Eval suite scores", ["suite", "metric"], buckets=(0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0))


def metrics_response():
    from starlette.responses import Response

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
