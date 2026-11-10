"""Metrics (Prometheus) and optional tracing (OpenTelemetry) for the HTTP API.

`install_observability(app, gate, settings)` is the one call `create_app` makes:

- `MetricsMiddleware`: request latency histogram and in-flight gauge per route
  template (never the raw path, so ids in URLs cannot blow up label cardinality).
- gate decisions by stage and outcome, and the audit chain length, read from the
  database at scrape time.
- `GET /metrics` in the Prometheus text format, protected by settings (see
  `scruple.observability.metrics.metrics_router`).
- when `SCRUPLE_OTEL_ENABLED=true`: a server span per request and trace ids on log
  lines (`scruple.observability.tracing`).
"""

from __future__ import annotations

from fastapi import FastAPI

from scruple.observability.metrics import GateCollector, MetricsMiddleware, metrics_router
from scruple.observability.tracing import TracingMiddleware, configure_tracing
from scruple.service import AgentGate
from scruple.settings import Settings


def install_observability(app: FastAPI, gate: AgentGate, settings: Settings) -> None:
    app.include_router(metrics_router(gate, settings))
    app.add_middleware(MetricsMiddleware)
    if settings.otel_enabled:
        provider = configure_tracing(settings)
        app.add_middleware(TracingMiddleware, tracer_provider=provider)


__all__ = [
    "GateCollector",
    "MetricsMiddleware",
    "TracingMiddleware",
    "configure_tracing",
    "install_observability",
    "metrics_router",
]
