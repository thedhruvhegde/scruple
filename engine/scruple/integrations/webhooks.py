"""Outbound webhooks for gate events: per-tenant endpoints, signed, retried, logged.

- Endpoints are registered by a tenant admin (`POST /integrations/webhooks`). The URL
  is checked against the SSRF rules (`scruple.integrations.safehttp`) when it is
  registered and again, pinned to the checked address, on every delivery.
- Each endpoint gets a generated signing secret (`whsec_` + 32 random bytes), shown
  once at creation and on rotation, stored sealed (`Runtime.seal`, AES-256-GCM with
  `SCRUPLE_FIELD_KEYS`, bound to tenant and endpoint). Rotation keeps the old secret
  signing alongside the new one for an overlap (default 24 hours), so a receiver can
  switch without dropping deliveries.
- Body: canonical JSON of the event (`api_version`, `id`, `type`, `tenant`,
  `created_at`, `audit_seq`, `audit_entry_hash`, `data`). Headers: `Scruple-Signature`
  (`t=...,v1=...`, `scruple.integrations.signing`), `Scruple-Event-Id`,
  `Scruple-Event-Type`, `Scruple-Delivery-Id`, `Scruple-Attempt`.
- Delivery runs as a `webhook_deliver` job (`scruple worker`). 2xx is delivered.
  A network error, 408, 429 or 5xx is retried by the job queue with its exponential
  backoff until `MAX_ATTEMPTS`; any other answer, a URL that now fails the address
  check, or a disabled endpoint fails the delivery at once. Receivers should
  deduplicate on `Scruple-Event-Id`: a delivery can arrive more than once (a timeout
  after the receiver processed it, a replay).
- Every attempt is logged (`webhook_attempts`: time, status code, error, duration,
  job); `replay` queues the same delivery again (same event id, new attempt) and
  records that it did.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scruple.clock import Clock, SystemClock
from scruple.hashing import canonical_json
from scruple.integrations.events import EVENT_TYPES
from scruple.integrations.runtime import Runtime
from scruple.integrations.safehttp import UnsafeURLError, check_url
from scruple.integrations.signing import SIGNATURE_HEADER, signature_header
from scruple.integrations.store import Endpoint, GateEvent, IntegrationStore
from scruple.jobs.queue import JobQueue
from scruple.jobs.worker import PermanentJobError
from scruple.service import AgentGate

WEBHOOK_KIND = "webhook_deliver"
MAX_ATTEMPTS = 8
TIMEOUT_S = 60
ROTATION_OVERLAP_S = 24 * 3600
API_VERSION = 1
RETRY_STATUSES = frozenset({408, 429})


class WebhookError(ValueError):
    pass


class TransientDeliveryError(RuntimeError):
    """Retry later (network error, 408, 429, 5xx)."""


class EndpointIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=8, max_length=2_048)
    events: tuple[str, ...] = Field(default=EVENT_TYPES, min_length=1, max_length=len(EVENT_TYPES))
    description: str = Field(default="", max_length=200)

    def checked_events(self) -> tuple[str, ...]:
        unknown = sorted(set(self.events) - set(EVENT_TYPES))
        if unknown:
            raise WebhookError(f"unknown event types {unknown} (one of {list(EVENT_TYPES)})")
        return tuple(e for e in EVENT_TYPES if e in self.events)


def _context(tenant: str, endpoint_id: str) -> str:
    return f"webhook-secret:{tenant}:{endpoint_id}"


def delivery_id(endpoint_id: str, event_id: str) -> str:
    return "whd_" + hashlib.sha256(f"{endpoint_id}\x00{event_id}".encode()).hexdigest()[:24]


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _check(url: str, runtime: Runtime) -> None:
    try:
        check_url(
            url,
            allow_http=runtime.http.allow_http,
            resolver=runtime.http.resolver,
            allow_hosts=runtime.http.allow_hosts,
        )
    except UnsafeURLError as exc:
        raise WebhookError(f"endpoint URL refused: {exc}") from None


def start_cursor(gate: AgentGate, store: IntegrationStore) -> None:
    """A tenant's first integration starts from now: history is not replayed to it."""
    with store.transaction():
        if not store.has_cursor():
            last = gate.audit.entries()
            store.set_cursor(last[-1].seq if last else 0)


def create_endpoint(
    gate: AgentGate, runtime: Runtime, body: EndpointIn, actor: str
) -> tuple[Endpoint, str]:
    """Register an endpoint. Returns it and its signing secret (shown only now)."""
    events = body.checked_events()
    _check(body.url, runtime)
    store = IntegrationStore(gate.storage).ensure()
    endpoint_id = "whe_" + secrets.token_hex(8)
    secret = new_secret()
    now = gate.clock.now()
    ep = Endpoint(
        endpoint_id=endpoint_id,
        url=body.url,
        events=events,
        secrets=(
            {
                "sealed": runtime.seal(secret, _context(store.tenant, endpoint_id)),
                "created_at": now.isoformat(),
                "retire_at": None,
            },
        ),
        active=True,
        description=body.description,
        created_by=actor,
        created_at=now,
    )
    with gate.storage.transaction():
        start_cursor(gate, store)
        store.add_endpoint(ep)
        gate.record_runtime_event(
            "integration.webhook_endpoint_created",
            actor,
            {"endpoint_id": endpoint_id, "url": body.url, "events": list(events)},
        )
    return ep, secret


def _get(store: IntegrationStore, endpoint_id: str) -> Endpoint:
    ep = store.endpoint(endpoint_id)
    if ep is None:
        raise KeyError(f"unknown endpoint {endpoint_id}")
    return ep


def rotate_secret(
    gate: AgentGate,
    runtime: Runtime,
    endpoint_id: str,
    actor: str,
    overlap_s: int = ROTATION_OVERLAP_S,
) -> str:
    """A new signing secret, first in line; the current ones retire after `overlap_s`.
    Secrets already retired are dropped."""
    store = IntegrationStore(gate.storage).ensure()
    ep = _get(store, endpoint_id)
    now = gate.clock.now()
    retire = (now + timedelta(seconds=max(0, overlap_s))).isoformat()
    kept = [
        {**s, "retire_at": s.get("retire_at") or retire}
        for s in ep.secrets
        if s.get("retire_at") is None or datetime.fromisoformat(s["retire_at"]) > now
    ]
    secret = new_secret()
    fresh = {
        "sealed": runtime.seal(secret, _context(store.tenant, endpoint_id)),
        "created_at": now.isoformat(),
        "retire_at": None,
    }
    with gate.storage.transaction():
        store.update_endpoint(endpoint_id, secrets=(fresh, *kept))
        gate.record_runtime_event(
            "integration.webhook_secret_rotated",
            actor,
            {"endpoint_id": endpoint_id, "old_secrets_retire_at": retire if kept else None},
        )
    return secret


def set_active(gate: AgentGate, endpoint_id: str, active: bool, actor: str) -> Endpoint:
    store = IntegrationStore(gate.storage).ensure()
    _get(store, endpoint_id)
    with gate.storage.transaction():
        store.update_endpoint(endpoint_id, active=active)
        gate.record_runtime_event(
            "integration.webhook_endpoint_" + ("enabled" if active else "disabled"),
            actor,
            {"endpoint_id": endpoint_id},
        )
    return _get(store, endpoint_id)


def signing_secrets(ep: Endpoint, runtime: Runtime, tenant: str, now: datetime) -> list[str]:
    return [
        runtime.open(s["sealed"], _context(tenant, ep.endpoint_id))
        for s in ep.secrets
        if s.get("retire_at") is None or datetime.fromisoformat(s["retire_at"]) > now
    ]


def body_of(event: GateEvent) -> bytes:
    return canonical_json(
        {"api_version": API_VERSION, **json.loads(event.model_dump_json())}
    ).encode("utf-8")


def enqueue(
    store: IntegrationStore, queue: JobQueue, ep: Endpoint, event: GateEvent, now: datetime
) -> str:
    did = delivery_id(ep.endpoint_id, event.id)
    store.add_delivery(did, ep.endpoint_id, event.id, now)
    queue.submit_raw(
        WEBHOOK_KIND,
        {"delivery_id": did},
        tenant_id=store.tenant,
        actor="integrations",
        idempotency_key=f"webhook:{did}",
        max_attempts=MAX_ATTEMPTS,
        timeout_s=TIMEOUT_S,
    )
    return did


def replay(gate: AgentGate, queue: JobQueue, delivery: str, actor: str) -> int:
    """Queue a delivery again (any status). Returns the replay number."""
    store = IntegrationStore(gate.storage).ensure()
    d = store.delivery(delivery)
    if d is None:
        raise KeyError(f"unknown delivery {delivery}")
    with gate.storage.transaction():
        n = store.mark_replay(delivery, gate.clock.now())
        gate.record_runtime_event(
            "integration.webhook_replayed",
            actor,
            {"delivery_id": delivery, "event_id": d.event_id, "replay": n},
        )
    queue.submit_raw(
        WEBHOOK_KIND,
        {"delivery_id": delivery},
        tenant_id=store.tenant,
        actor=actor,
        idempotency_key=f"webhook:{delivery}:replay-{n}",
        max_attempts=MAX_ATTEMPTS,
        timeout_s=TIMEOUT_S,
    )
    return n


def deliver(
    storage: object,
    runtime: Runtime,
    delivery: str,
    *,
    job_id: str = "",
    attempt: int = 1,
    max_attempts: int = MAX_ATTEMPTS,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """One attempt. Raises TransientDeliveryError to be retried, PermanentJobError to
    stop; returns the result on success."""
    clock = clock or SystemClock()
    store = IntegrationStore(storage).ensure()
    d = store.delivery(delivery)
    if d is None:
        raise PermanentJobError(f"unknown delivery {delivery}")
    ep, event = store.endpoint(d.endpoint_id), store.event(d.event_id)
    last = attempt >= max_attempts
    now = clock.now()

    def log(code: int | None, error: str | None, ms: int, final: str | None) -> None:
        store.record_attempt(
            delivery,
            now=now,
            status_code=code,
            error=error[:500] if error else None,
            duration_ms=ms,
            job_id=job_id,
            final=final,
        )

    if ep is None or event is None:
        log(None, "endpoint or event missing", 0, "failed")
        raise PermanentJobError("endpoint or event missing")
    if not ep.active:
        log(None, "endpoint disabled", 0, "failed")
        raise PermanentJobError("endpoint disabled")
    body = body_of(event)
    try:
        keys = signing_secrets(ep, runtime, store.tenant, now)
    except Exception as exc:  # a key problem: never retried blindly
        log(None, f"cannot open signing secret: {type(exc).__name__}", 0, "failed")
        raise PermanentJobError("cannot open the endpoint's signing secret") from None
    t = int(now.timestamp())
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature_header(keys, t, body),
        "Scruple-Event-Id": event.id,
        "Scruple-Event-Type": event.type,
        "Scruple-Delivery-Id": delivery,
        "Scruple-Attempt": str(d.attempts + 1),
    }
    started = time.monotonic()
    try:
        resp = runtime.http.post(ep.url, headers=headers, content=body)
    except UnsafeURLError as exc:
        log(None, f"refused: {exc}", 0, "failed")
        raise PermanentJobError(f"endpoint URL refused: {exc}") from None
    except httpx.HTTPError as exc:
        ms = int((time.monotonic() - started) * 1000)
        log(None, f"{type(exc).__name__}: {exc}", ms, "failed" if last else None)
        raise TransientDeliveryError(f"{type(exc).__name__}") from None
    ms = int((time.monotonic() - started) * 1000)
    code = resp.status_code
    if 200 <= code < 300:
        log(code, None, ms, "delivered")
        return {"delivery_id": delivery, "status_code": code, "event_id": event.id}
    snippet = resp.body[:200].decode("utf-8", "replace")
    if code in RETRY_STATUSES or code >= 500:
        log(code, f"HTTP {code}: {snippet}", ms, "failed" if last else None)
        raise TransientDeliveryError(f"HTTP {code}")
    log(code, f"HTTP {code}: {snippet}", ms, "failed")
    raise PermanentJobError(f"HTTP {code}")


__all__ = [
    "MAX_ATTEMPTS",
    "WEBHOOK_KIND",
    "EndpointIn",
    "TransientDeliveryError",
    "WebhookError",
    "body_of",
    "create_endpoint",
    "deliver",
    "delivery_id",
    "enqueue",
    "replay",
    "rotate_secret",
    "set_active",
    "signing_secrets",
    "start_cursor",
]
