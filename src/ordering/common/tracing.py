"""OpenTelemetry setup and W3C trace-context propagation helpers.

Services export OTLP to ``OTEL_EXPORTER_OTLP_ENDPOINT``: for the API gateway and agent
service this is the collector *sidecar* on localhost; others use the central collector.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import metrics, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from ordering.common.config import Settings

_initialized = False


def setup_tracing(settings: Settings, app: Any | None = None, sqlalchemy_engine: Any | None = None) -> None:
    global _initialized
    if not _initialized:
        resource = Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": settings.agent_version,
                "deployment.environment": settings.env,
            }
        )
        provider = TracerProvider(resource=resource)
        if settings.otel_enabled:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True))
            )
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
            )
            metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
        trace.set_tracer_provider(provider)
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except Exception:  # noqa: S110  # nosec B110 - optional instrumentation
            pass
        _initialized = True
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    if sqlalchemy_engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(engine=sqlalchemy_engine.sync_engine)
        except Exception:  # noqa: S110  # nosec B110 - optional instrumentation
            pass


def tracer(name: str = "ordering"):
    return trace.get_tracer(name)


def inject_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Add ``traceparent``/``tracestate`` for the current span to outgoing headers."""
    headers = dict(headers or {})
    propagate.inject(headers)
    return headers


def extract_context(headers: dict[str, str]):
    return propagate.extract(headers)


def current_trace_id() -> str | None:
    span_ctx = trace.get_current_span().get_span_context()
    return format(span_ctx.trace_id, "032x") if span_ctx.is_valid else None
