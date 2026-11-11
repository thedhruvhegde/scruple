"""Outbound webhooks end to end: dispatch from the audit log, signed deliveries through a
thread worker, retries, the delivery log, replay, rotation, SSRF, tenancy and the API.
Synthetic data; every request goes to an httpx.MockTransport."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import Role, key_permissions
from scruple.integrations.config import IntegrationSettings, SlackConfig
from scruple.integrations.dispatch import Dispatcher, dispatch
from scruple.integrations.jobs import make_handlers
from scruple.integrations.runtime import Runtime, SealError
from scruple.integrations.safehttp import SafeHttp
from scruple.integrations.signing import SIGNATURE_HEADER, sign_webhook, verify_webhook
from scruple.integrations.store import IntegrationStore
from scruple.integrations.webhooks import (
    EndpointIn,
    WebhookError,
    body_of,
    create_endpoint,
    replay,
    rotate_secret,
    set_active,
)
from scruple.jobs.queue import JobQueue, queue_for
from scruple.jobs.worker import JobContext, Worker
from scruple.security.crypto import FieldCipher
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage

T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
PUBLIC = "93.184.215.14"
APPROVER = "model-risk@demo.bank"
URL = "https://hooks.example.com/scruple"


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class Receiver:
    """A scripted receiver: answers from `script` (then 200), records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.script: list[int | Exception] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        nxt = self.script.pop(0) if self.script else 200
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(nxt, content=b"ok" if nxt < 300 else b"nope")


class World:
    def __init__(self, tmp: Path, dns: Callable[[str, int], list[str]] | None = None) -> None:
        self.path = str(tmp / "gate.db")
        self.clock = Clock()
        self.storage = SQLiteStorage(self.path)
        self.gate = AgentGate(self.storage, self.clock)
        self.gate.bootstrap_baseline()
        self.receiver = Receiver()
        self.dns = dns or (lambda h, p: [PUBLIC])
        self.runtime = Runtime(
            http=SafeHttp(transport=httpx.MockTransport(self.receiver), resolver=self._resolve),
            cipher=FieldCipher({"k1": b"\x07" * 32}, "k1"),
        )
        self.queue: JobQueue = queue_for(self.storage, clock=self.clock)
        self.worker = Worker(
            self.queue,
            JobContext(database=self.path),
            handlers=make_handlers(self.runtime, self.clock),
            isolation="thread",
            lease_s=30,
        )

    def _resolve(self, host: str, port: int) -> list[str]:
        return self.dns(host, port)

    def decision(self, reason: str = "roll back for the webhook test") -> str:
        rid = self.gate.registry.active_rules()[0].rule_id
        return self.gate.rollback(rid, APPROVER, reason).decision.decision_id

    def drain(self) -> int:
        ran = 0
        while self.worker.run_once() is not None:
            ran += 1
        return ran


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


def _endpoint(w: World, events: tuple[str, ...] | None = None) -> tuple[str, str]:
    body = EndpointIn(url=URL) if events is None else EndpointIn(url=URL, events=events)
    ep, secret = create_endpoint(w.gate, w.runtime, body, "admin")
    return ep.endpoint_id, secret


def test_a_decision_is_delivered_signed(w: World) -> None:
    w.decision("before any endpoint")  # history is not replayed to a new endpoint
    eid, secret = _endpoint(w)
    assert secret.startswith("whsec_")
    did = w.decision()
    res = dispatch(w.gate, w.queue)
    assert res.events == 1 and res.jobs == 1
    assert w.drain() == 1
    (req,) = w.receiver.requests
    body = req.content
    t = verify_webhook(req.headers[SIGNATURE_HEADER], body, [secret], now=T0.timestamp())
    assert t == int(T0.timestamp())
    event = json.loads(body)
    assert event["type"] == "decision.recorded" and event["data"]["decision_id"] == did
    assert event["data"]["data"] == "synthetic" and event["api_version"] == 1
    assert req.headers["scruple-event-id"] == event["id"]
    assert req.headers["scruple-event-type"] == "decision.recorded"
    assert req.headers["scruple-attempt"] == "1"
    assert body == json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    store = IntegrationStore(w.storage)
    (d,) = store.deliveries()
    assert d.status == "delivered" and d.attempts == 1 and d.last_status_code == 200
    assert d.endpoint_id == eid and d.delivered_at == T0
    (a,) = store.attempts(d.delivery_id)
    assert a.status_code == 200 and a.error is None
    # dispatching again finds nothing new and queues nothing twice
    assert dispatch(w.gate, w.queue).jobs == 0
    assert w.drain() == 0
    assert w.gate.verify_integrity().ok


def test_the_secret_is_stored_sealed(w: World) -> None:
    _, secret = _endpoint(w)
    rows = w.storage.connection.query("SELECT secrets_json FROM webhook_endpoints")
    assert secret not in rows[0]["secrets_json"] and "enc:v1:k1:" in rows[0]["secrets_json"]
    listed = IntegrationStore(w.storage).endpoints()[0].public()
    assert "sealed" not in json.dumps(listed)
    no_keys = Runtime(http=w.runtime.http)
    with pytest.raises(SealError):
        create_endpoint(w.gate, no_keys, EndpointIn(url=URL), "admin")
    dev = Runtime(http=w.runtime.http, dev=True)
    ep, secret = create_endpoint(w.gate, dev, EndpointIn(url=URL), "admin")
    assert dev.open(ep.secrets[0]["sealed"], "x") == secret
    with pytest.raises(SealError):  # an unsealed secret outside development mode
        no_keys.open(ep.secrets[0]["sealed"], "x")


def test_server_errors_are_retried_with_backoff_then_delivered(w: World) -> None:
    _endpoint(w)
    w.decision()
    dispatch(w.gate, w.queue)
    w.receiver.script = [503, httpx.ConnectError("refused"), 429]
    assert w.drain() == 1  # first attempt fails; the retry waits for its backoff
    for _ in range(3):
        w.clock.advance(minutes=10)
        w.drain()
    store = IntegrationStore(w.storage)
    (d,) = store.deliveries()
    assert d.status == "delivered" and d.attempts == 4
    codes = [a.status_code for a in store.attempts(d.delivery_id)]
    assert codes == [503, None, 429, 200]
    assert "ConnectError" in (store.attempts(d.delivery_id)[1].error or "")
    assert [r.headers["scruple-attempt"] for r in w.receiver.requests] == ["1", "2", "3", "4"]
    assert len({r.headers["scruple-event-id"] for r in w.receiver.requests}) == 1


def test_client_errors_fail_at_once(w: World) -> None:
    _endpoint(w)
    w.decision()
    dispatch(w.gate, w.queue)
    w.receiver.script = [400]
    w.drain()
    w.clock.advance(hours=1)
    assert w.drain() == 0
    (d,) = IntegrationStore(w.storage).deliveries()
    assert d.status == "failed" and d.attempts == 1 and "HTTP 400" in (d.last_error or "")
    job = w.queue.list_jobs()[-1]
    assert job.status.value == "failed" and job.attempts == 1


def test_attempts_run_out(w: World) -> None:
    _endpoint(w)
    w.decision()
    dispatch(w.gate, w.queue)
    w.receiver.script = [500] * 20
    for _ in range(12):
        w.drain()
        w.clock.advance(minutes=10)
    (d,) = IntegrationStore(w.storage).deliveries()
    assert d.status == "failed" and d.attempts == 8
    assert len(w.receiver.requests) == 8


def test_replay_sends_the_same_event_again(w: World) -> None:
    _endpoint(w)
    w.decision()
    dispatch(w.gate, w.queue)
    w.receiver.script = [404]
    w.drain()
    store = IntegrationStore(w.storage)
    (d,) = store.deliveries(status="failed")
    assert replay(w.gate, w.queue, d.delivery_id, "admin") == 1
    assert w.drain() == 1
    d2 = store.delivery(d.delivery_id)
    assert d2 is not None and d2.status == "delivered" and d2.replays == 1
    ids = [r.headers["scruple-event-id"] for r in w.receiver.requests]
    assert len(ids) == 2 and ids[0] == ids[1]
    assert replay(w.gate, w.queue, d.delivery_id, "admin") == 2  # replays never collide
    assert w.drain() == 1
    events = [e.event_type for e in w.gate.audit.entries()]
    assert events.count("integration.webhook_replayed") == 2
    with pytest.raises(KeyError):
        replay(w.gate, w.queue, "whd_" + "0" * 24, "admin")


def test_rotation_signs_with_both_secrets_for_the_overlap(w: World) -> None:
    eid, old = _endpoint(w)
    new = rotate_secret(w.gate, w.runtime, eid, "admin")
    w.decision()
    dispatch(w.gate, w.queue)
    w.drain()
    req = w.receiver.requests[-1]
    header = req.headers[SIGNATURE_HEADER]
    assert header.count("v1=") == 2
    for s in (old, new):
        verify_webhook(header, req.content, [s], now=w.clock.t.timestamp())
    w.clock.advance(hours=25)  # the old secret retires
    w.decision("second rollback")
    dispatch(w.gate, w.queue)
    w.drain()
    header = w.receiver.requests[-1].headers[SIGNATURE_HEADER]
    assert header.count("v1=") == 1
    verify_webhook(header, w.receiver.requests[-1].content, [new], now=w.clock.t.timestamp())
    newer = rotate_secret(w.gate, w.runtime, eid, "admin")  # drops the retired secret
    ep = IntegrationStore(w.storage).endpoint(eid)
    assert ep is not None and len(ep.secrets) == 2 and newer not in (old, new)
    with pytest.raises(KeyError):
        rotate_secret(w.gate, w.runtime, "whe_" + "0" * 16, "admin")


def test_subscriptions_and_disabled_endpoints(w: World) -> None:
    only_approvals, _ = _endpoint(w, ("approval.requested",))
    every, _ = _endpoint(w)
    w.decision()
    assert dispatch(w.gate, w.queue).jobs == 1  # only the endpoint that wants decisions
    set_active(w.gate, every, False, "admin")
    w.drain()
    (d,) = IntegrationStore(w.storage).deliveries()
    assert d.endpoint_id == every and d.status == "failed" and d.last_error == "endpoint disabled"
    assert w.receiver.requests == []
    w.decision("another")
    assert dispatch(w.gate, w.queue).jobs == 0  # disabled endpoints get nothing new
    set_active(w.gate, every, True, "admin")
    with pytest.raises(WebhookError, match="unknown event"):
        create_endpoint(w.gate, w.runtime, EndpointIn(url=URL, events=("rule.x",)), "admin")
    assert only_approvals
    with pytest.raises(KeyError):
        set_active(w.gate, "whe_" + "0" * 16, True, "admin")


def test_urls_are_checked_at_registration_and_delivery(tmp_path: Path) -> None:
    answers = {"hooks.example.com": [PUBLIC]}
    w = World(tmp_path, dns=lambda h, p: answers[h])
    for bad in ("http://hooks.example.com/", "https://127.0.0.1/", "https://10.0.0.1:8443/"):
        with pytest.raises(WebhookError, match="refused"):
            create_endpoint(w.gate, w.runtime, EndpointIn(url=bad), "admin")
    _endpoint(w)
    w.decision()
    dispatch(w.gate, w.queue)
    answers["hooks.example.com"] = ["169.254.169.254"]  # rebinding after registration
    w.drain()
    (d,) = IntegrationStore(w.storage).deliveries()
    assert d.status == "failed" and "blocked address" in (d.last_error or "")
    assert w.receiver.requests == []


def test_tenants_see_only_their_own_events(tmp_path: Path) -> None:
    w = World(tmp_path)
    b = AgentGate(w.storage.for_tenant("bank-b"), w.clock)
    b.bootstrap_baseline()
    create_endpoint(b, w.runtime, EndpointIn(url="https://b.example.com/x"), "admin-b")
    w.decision()  # tenant "default" has no endpoint
    results = Dispatcher(w.queue, w.storage, interval_s=0).tick()
    assert [r.tenant for r in results] == ["bank-b"] and results[0].jobs == 0
    b.rollback(b.registry.active_rules()[0].rule_id, APPROVER, "b's own decision")
    (res,) = Dispatcher(w.queue, w.storage, interval_s=0).tick()
    assert res.jobs == 1
    w.drain()
    (req,) = w.receiver.requests
    assert req.headers["host"] == "b.example.com" and json.loads(req.content)["tenant"] == "bank-b"
    assert IntegrationStore(w.storage).deliveries() == []  # default's view is empty


def test_dispatcher_throttles(w: World) -> None:
    now = [0.0]
    d = Dispatcher(w.queue, w.storage, interval_s=5, now=lambda: now[0])
    _endpoint(w)
    assert len(d.tick()) == 1
    now[0] = 4.0
    assert d.tick() == []
    now[0] = 5.0
    assert len(d.tick()) == 1


def test_documented_verification_snippet() -> None:
    text = (Path(__file__).parent.parent / "docs" / "INTEGRATIONS.md").read_text()
    code = re.search(r"```python\n# scruple-webhook-verify\n(.*?)```", text, re.S)
    assert code is not None
    ns: dict[str, Any] = {}
    exec(code.group(1), ns)
    body = b'{"api_version":1,"id":"evt_x"}'
    t = int(time.time())
    header = f"t={t},v1={sign_webhook('whsec_a', t, body)},v1={sign_webhook('whsec_b', t, body)}"
    assert ns["verify"]("whsec_a", header, body) and ns["verify"]("whsec_b", header, body)
    assert not ns["verify"]("whsec_c", header, body)
    assert not ns["verify"]("whsec_a", header, body + b" ")
    stale = f"t={t - 600},v1={sign_webhook('whsec_a', t - 600, body)}"
    assert not ns["verify"]("whsec_a", stale, body)
    assert not ns["verify"]("whsec_a", "garbage", body)


def test_body_is_canonical(w: World) -> None:
    _endpoint(w)
    w.decision()
    dispatch(w.gate, w.queue)
    (event,) = IntegrationStore(w.storage).events()
    assert json.loads(body_of(event))["id"] == event.id


# --- HTTP ---------------------------------------------------------------------------


def test_webhook_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(tmp_path)
    keys = ApiKeyService(w.storage, params=ScryptParams(n=1024), permissions_for=key_permissions)
    hdr = {}
    for subject, role in (("auditor", Role.AUDITOR), ("admin", Role.RISK_ADMIN)):
        _, tok = keys.create(subject, [], "local-admin", roles=[role])
        hdr[subject] = {"X-API-Key": tok.get_secret_value()}
    from scruple.integrations import api as api_mod

    real = api_mod.integrations_router
    monkeypatch.setattr(
        api_mod,
        "integrations_router",
        lambda gates, queue, runtime=None: real(gates, w.queue, w.runtime),
    )
    c = TestClient(create_app(w.gate, auth=Authenticator(keys)))
    admin, auditor = hdr["admin"], hdr["auditor"]
    assert c.get("/integrations/webhooks", headers=auditor).status_code == 403
    r = c.post("/integrations/webhooks", json={"url": URL}, headers=admin)
    assert r.status_code == 201, r.text
    secret, eid = r.json()["secret"], r.json()["endpoint"]["endpoint_id"]
    assert secret not in c.get("/integrations/webhooks", headers=admin).text
    r = c.post("/integrations/webhooks", json={"url": "https://10.0.0.1/"}, headers=admin)
    assert r.status_code == 422
    assert (
        c.post("/integrations/webhooks", json={"url": URL, "x": 1}, headers=admin).status_code
        == 422
    )
    w.decision()
    assert c.post("/integrations/dispatch", headers=admin).json()["jobs"] == 1
    w.receiver.script = [500]
    w.drain()
    (d,) = c.get("/integrations/webhooks/deliveries?status=pending", headers=admin).json()
    one = c.get(f"/integrations/webhooks/deliveries/{d['delivery_id']}", headers=admin).json()
    assert one["attempts_log"][0]["status_code"] == 500
    r = c.post(f"/integrations/webhooks/deliveries/{d['delivery_id']}/replay", headers=admin)
    assert r.json()["replay"] == 1
    w.drain()
    assert c.get("/integrations/webhooks/deliveries?status=delivered", headers=admin).json()
    assert (
        c.get("/integrations/webhooks/deliveries/whd_" + "1" * 24, headers=admin).status_code == 404
    )
    assert (
        c.post(
            "/integrations/webhooks/deliveries/whd_" + "1" * 24 + "/replay", headers=admin
        ).status_code
        == 404
    )
    assert c.get("/integrations/webhooks/deliveries/nope", headers=admin).status_code == 422
    r = c.post(f"/integrations/webhooks/{eid}/rotate", headers=admin)
    assert r.json()["secret"].startswith("whsec_") and r.json()["secret"] != secret
    assert c.post(f"/integrations/webhooks/{eid}/disable", headers=admin).json()["active"] is False
    assert c.post(f"/integrations/webhooks/{eid}/enable", headers=admin).json()["active"] is True
    missing = "whe_" + "0" * 16
    assert c.post(f"/integrations/webhooks/{missing}/disable", headers=admin).status_code == 404
    assert c.post(f"/integrations/webhooks/{missing}/rotate", headers=admin).status_code == 404
    # settings: secrets must be the tenant's own variables
    slack = {
        "team_id": "T0001",
        "channel": "C0001",
        "bot_token_env": "SCRUPLE_SECRET_DEFAULT_SLACK_BOT",
        "signing_secret_envs": ["SCRUPLE_SECRET_DEFAULT_SLACK_SIGNING"],
    }
    r = c.post("/integrations/settings", json={"slack": slack}, headers=admin)
    assert r.status_code == 200 and r.json()["number"] == 1
    other = {**slack, "bot_token_env": "SCRUPLE_SECRET_BANK_B_SLACK_BOT"}
    assert c.post("/integrations/settings", json={"slack": other}, headers=admin).status_code == 422
    got = c.get("/integrations/settings", headers=admin).json()
    assert IntegrationSettings.model_validate(got).slack == SlackConfig.model_validate(slack)
    kinds = [
        e.event_type for e in w.gate.audit.entries() if e.event_type.startswith("integration.")
    ]
    assert kinds == [
        "integration.webhook_endpoint_created",
        "integration.webhook_replayed",
        "integration.webhook_secret_rotated",
        "integration.webhook_endpoint_disabled",
        "integration.webhook_endpoint_enabled",
        "integration.settings_set",
    ]
    assert w.gate.verify_integrity().ok
