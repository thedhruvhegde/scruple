"""OpenTelemetry tracing: off by default; when on, one SERVER span per request,
W3C traceparent continued, request id and route recorded, trace ids on log lines."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import scruple.observability as obs
from scruple.api.app import create_app
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.logging import configure_logging
from scruple.observability.tracing import TracingMiddleware, configure_tracing
from scruple.service import AgentGate
from scruple.settings import Settings
from scruple.storage import SQLiteStorage

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"


def _gate() -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), GateConfig(n_cases=500))
    g.bootstrap_baseline()
    return g


@pytest.fixture
def traced() -> tuple[TestClient, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = configure_tracing(Settings(otel_enabled=True), exporter, set_global=False)
    # Development-only X-Actor trust, so a protected route can be traced.
    app = create_app(_gate(), settings=Settings(auth_dev_actor_header=True))
    app.add_middleware(TracingMiddleware, tracer_provider=provider)
    return TestClient(app, headers={"X-Actor": "auditor"}), exporter


def _middleware(app: FastAPI) -> list[Any]:
    return [m.cls for m in app.user_middleware]


def test_tracing_is_off_by_default() -> None:
    assert TracingMiddleware not in _middleware(create_app(_gate(), settings=Settings()))


def test_enabling_tracing_installs_the_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    real = obs.configure_tracing

    def fake(settings: Settings, exporter_: object = None, set_global: bool = True) -> object:
        return real(settings, exporter, set_global=False)

    monkeypatch.setattr(obs, "configure_tracing", fake)
    app = create_app(_gate(), settings=Settings(otel_enabled=True))
    assert TracingMiddleware in _middleware(app)
    TestClient(app).get("/health")
    assert [s.name for s in exporter.get_finished_spans()] == ["GET /health"]


def test_one_server_span_per_request_named_by_route(
    traced: tuple[TestClient, InMemorySpanExporter],
) -> None:
    client, exporter = traced
    r = client.get("/rules/no-such-rule")
    assert r.status_code == 404
    (span,) = exporter.get_finished_spans()
    assert span.name == "GET /rules/{rule_id}"
    assert span.kind == trace.SpanKind.SERVER
    attrs = dict(span.attributes or {})
    assert attrs["http.route"] == "/rules/{rule_id}"
    assert attrs["http.response.status_code"] == 404
    assert attrs["url.path"] == "/rules/no-such-rule"
    assert attrs["scruple.request_id"] == r.headers["x-request-id"]


def test_incoming_traceparent_is_continued(traced: tuple[TestClient, InMemorySpanExporter]) -> None:
    client, exporter = traced
    client.get("/health", headers={"traceparent": f"00-{TRACE_ID}-{PARENT}-01"})
    (span,) = exporter.get_finished_spans()
    assert format(span.context.trace_id, "032x") == TRACE_ID
    assert span.parent is not None and format(span.parent.span_id, "016x") == PARENT


def test_server_errors_mark_the_span() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = FastAPI()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    app.add_middleware(TracingMiddleware, tracer_provider=provider)
    TestClient(app, raise_server_exceptions=False).get("/boom")
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR


def test_log_lines_carry_the_trace_id(traced: tuple[TestClient, InMemorySpanExporter]) -> None:
    client, exporter = traced
    stream = io.StringIO()
    handler = configure_logging("INFO", "json", stream)
    try:
        client.get("/health")
    finally:
        logging.getLogger().removeHandler(handler)
    (span,) = exporter.get_finished_spans()
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    access = [x for x in lines if x.get("msg") == "request" and x.get("path") == "/health"]
    assert access and access[-1]["trace_id"] == format(span.context.trace_id, "032x")
    assert access[-1]["span_id"] == format(span.context.span_id, "016x")
