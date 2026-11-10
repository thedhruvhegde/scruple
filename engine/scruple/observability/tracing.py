"""Optional OpenTelemetry tracing, off unless `SCRUPLE_OTEL_ENABLED=true`.

Enabled, `configure_tracing` installs an SDK tracer provider that exports spans
over OTLP/HTTP (endpoint and headers from the standard OTEL_EXPORTER_OTLP_* env
vars; `SCRUPLE_OTEL_SERVICE_NAME` names the service), `TracingMiddleware` opens one
SERVER span per HTTP request (continuing an incoming W3C `traceparent`), and every
log record gains `trace_id` and `span_id` fields while a span is active.

The SDK and exporter are the `otel` extra (`uv sync --extra otel`; the image has
them). The API package alone records nothing, so tracing costs nothing when off.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from opentelemetry import propagate, trace

from scruple.settings import Settings

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

TRACER_NAME = "scruple.api"
_factory_installed = False


class TracingUnavailableError(RuntimeError):
    pass


def configure_tracing(settings: Settings, exporter: Any = None, set_global: bool = True) -> Any:
    """Build a tracer provider (installed globally unless `set_global=False`).

    `exporter` replaces the OTLP exporter (tests use an in-memory one)."""
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise TracingUnavailableError(
            "SCRUPLE_OTEL_ENABLED=true needs the otel extra: uv sync --extra otel"
        ) from exc
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    if exporter is None:  # pragma: no cover - needs a collector
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    if set_global:
        trace.set_tracer_provider(provider)
    install_log_correlation()
    return provider


def install_log_correlation() -> None:
    """Add trace_id/span_id to every log record created inside a recording span."""
    global _factory_installed
    if _factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            record.trace_id = format(ctx.trace_id, "032x")
            record.span_id = format(ctx.span_id, "016x")
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class TracingMiddleware:
    """One SERVER span per HTTP request, named `METHOD /route/{template}`."""

    def __init__(self, app: ASGIApp, tracer_provider: Any = None) -> None:
        self.app = app
        self.tracer = trace.get_tracer(TRACER_NAME, tracer_provider=tracer_provider)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from scruple.observability.metrics import route_template

        carrier = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = str(scope.get("method", "GET"))
        with self.tracer.start_as_current_span(
            method,
            context=propagate.extract(carrier),
            kind=trace.SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": str(scope.get("path", ""))},
        ) as span:

            async def send_status(message: Message) -> None:
                if message["type"] == "http.response.start":
                    for k, v in message.get("headers", []):
                        if k.lower() == b"x-request-id":
                            span.set_attribute("scruple.request_id", v.decode("latin-1"))
                    code = int(message["status"])
                    span.set_attribute("http.response.status_code", code)
                    if code >= 500:
                        span.set_status(trace.StatusCode.ERROR)
                await send(message)

            try:
                await self.app(scope, receive, send_status)
            finally:
                route = route_template(scope)
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
