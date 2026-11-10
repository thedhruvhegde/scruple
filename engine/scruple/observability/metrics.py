"""Prometheus metrics for the engine.

Metrics (all names prefixed `scruple_`):

| name | type | labels | meaning |
|---|---|---|---|
| http_request_duration_seconds | histogram | method, route, status | wall time per request |
| http_requests_in_flight | gauge | method | requests being served now |
| gate_decisions_total | counter | stage, outcome | gate decisions committed to the database |
| audit_chain_length | gauge | | entries in the audit hash chain |
| build_info | gauge | version, commit | always 1 |

The two gate metrics are read from the database at scrape time (the audit log is
the record of every decision), so they count only committed decisions, agree across
replicas and survive restarts. Decisions are read incrementally: each scrape reads
only audit entries newer than the last one it saw.

`route` is the matched route template (`/rules/{rule_id}`), `unmatched` for 404s
that hit no route. Under gunicorn with several workers set PROMETHEUS_MULTIPROC_DIR
to an empty writable directory (the image and the Helm chart use /tmp/metrics):
each worker then writes its samples there and a scrape aggregates all workers.

Access (settings, env only for the token):
- `SCRUPLE_METRICS_TOKEN` set: `Authorization: Bearer <token>` is required.
- otherwise `SCRUPLE_METRICS_PUBLIC=true` serves it without auth (only behind a
  NetworkPolicy or on a private network);
- otherwise /metrics answers 404: off unless configured.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric

from scruple.auth import signed
from scruple.service import AgentGate
from scruple.settings import Settings
from scruple.storage.sql import SQLStorage
from scruple.version import build_info

MULTIPROC_ENV = "PROMETHEUS_MULTIPROC_DIR"
if _multiproc_dir := os.environ.get(MULTIPROC_ENV):
    # Must exist before the first metric is created below (gunicorn's on_starting
    # also creates and empties it; this covers `scruple serve` and tests).
    os.makedirs(_multiproc_dir, exist_ok=True)

DECISION_EVENT = "decision.recorded"
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

REQUEST_LATENCY = Histogram(
    "scruple_http_request_duration_seconds",
    "HTTP request wall time by route template.",
    ["method", "route", "status"],
    buckets=LATENCY_BUCKETS,
)
IN_FLIGHT = Gauge(
    "scruple_http_requests_in_flight",
    "HTTP requests being served.",
    ["method"],
    multiprocess_mode="livesum",
)
BUILD_INFO = Gauge(
    "scruple_build_info",
    "Build of the running engine (always 1).",
    ["version", "commit"],
    multiprocess_mode="max",
)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class MetricsMiddleware:
    """Latency histogram and in-flight gauge for every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET"))
        status = 500
        started = time.perf_counter()

        async def send_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        IN_FLIGHT.labels(method).inc()
        try:
            await self.app(scope, receive, send_status)
        finally:
            IN_FLIGHT.labels(method).dec()
            REQUEST_LATENCY.labels(method, route_template(scope), str(status)).observe(
                time.perf_counter() - started
            )


class GateCollector:
    """`scruple_gate_decisions_total` and `scruple_audit_chain_length` from storage."""

    def __init__(self, gate: AgentGate) -> None:
        self.gate = gate
        self._lock = threading.Lock()
        self._seen = 0
        self._counts: Counter[tuple[str, str]] = Counter()

    def _read_new(self) -> None:
        storage = self.gate.storage
        if isinstance(storage, SQLStorage):
            rows = storage._conn.query(
                "SELECT seq, payload_json FROM audit_events "
                "WHERE event_type = ? AND seq > ? ORDER BY seq",
                (DECISION_EVENT, self._seen),
            )
            entries = [(int(r["seq"]), json.loads(r["payload_json"])) for r in rows]
        else:  # any other Storage: a full read, still exact
            entries = [
                (e.seq, e.payload)
                for e in storage.list_audit()
                if e.event_type == DECISION_EVENT and e.seq > self._seen
            ]
        for seq, payload in entries:
            self._counts[(str(payload.get("stage")), str(payload.get("outcome")))] += 1
            self._seen = seq

    def collect(self) -> Iterator[Metric]:
        with self._lock:
            self._read_new()
            counts = dict(self._counts)
        decisions = CounterMetricFamily(
            "scruple_gate_decisions",
            "Gate decisions committed to the database (all replicas), from the audit log.",
            labels=["stage", "outcome"],
        )
        for (stage, outcome), n in sorted(counts.items()):
            decisions.add_metric([stage, outcome], n)
        yield decisions
        last = self.gate.storage.last_audit()
        yield GaugeMetricFamily(
            "scruple_audit_chain_length",
            "Entries in the audit hash chain.",
            value=0 if last is None else last.seq,
        )


def _registry() -> CollectorRegistry:
    if os.environ.get(MULTIPROC_ENV):
        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)  # type: ignore[no-untyped-call]
        return reg
    return REGISTRY


def render(gate_metrics: GateCollector) -> bytes:
    info = build_info()
    BUILD_INFO.labels(info.version, info.commit[:12]).set(1)
    per_scrape = CollectorRegistry()
    per_scrape.register(gate_metrics)
    return generate_latest(_registry()) + generate_latest(per_scrape)


METRICS_PATH = "/metrics"


def _authorized(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        value.strip().encode(), token.encode()
    )


def metrics_router(gate: AgentGate, settings: Settings) -> APIRouter:
    router = APIRouter()
    token = settings.metrics_token.get_secret_value() if settings.metrics_token else None
    gate_metrics = GateCollector(gate)

    # `signed`: the route checks its own scrape token (not an API key), and the auth
    # middleware leaves this path's Authorization header to it.
    @router.get(METRICS_PATH, include_in_schema=False, dependencies=[signed("metrics-token")])
    def metrics(request: Request) -> Response:
        if token is not None:
            if not _authorized(request, token):
                raise HTTPException(
                    401, "metrics need a bearer token", {"WWW-Authenticate": "Bearer"}
                )
        elif not settings.metrics_public:
            raise HTTPException(404, "Not Found")
        return Response(render(gate_metrics), media_type=CONTENT_TYPE_LATEST)

    return router
