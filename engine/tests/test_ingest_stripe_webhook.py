"""Stripe webhook signatures: fixed vectors (computed independently with openssl),
replay and tolerance attacks, malformed headers, and the FastAPI route."""

from __future__ import annotations

import contextlib
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.ingest.api import SECRETS_ENV, ingest_router
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    LiveModeRefusedError,
    SchemaMismatchError,
    SignatureError,
)
from scruple.ingest.stripe.webhook import (
    DISPUTE_EVENT_TYPES,
    StripeWebhookReceiver,
    compute_signature,
    parse_signature_header,
    sign_header,
    verify_signature,
)
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage

FIX = Path(__file__).parent / "fixtures" / "stripe"
SPEC = json.loads((FIX / "spec3.subset.json").read_text())
RES: dict[str, Any] = json.loads((FIX / "fixtures3.subset.json").read_text())["resources"]
SECRET = "whsec_test_secret_0123456789"
OTHER = "whsec_other_secret_9876543210"
T0 = 1_700_000_000


class FixedClock:
    def __init__(self, t: int) -> None:
        self.t = t

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)


def event_body(event_type: str = "charge.dispute.created", **over: Any) -> bytes:
    ev: dict[str, Any] = copy.deepcopy(RES["event"])
    ev.update(type=event_type, id=over.pop("id", "evt_test_1"), data={"object": RES["dispute"]})
    ev.update(over)
    return json.dumps(ev).encode()


def receiver(t: int = T0, **kw: Any) -> StripeWebhookReceiver:
    return StripeWebhookReceiver([SECRET], clock=FixedClock(t), **kw)


# --- vectors --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            b'{"id":"evt_test","object":"event"}',
            "f01d57453e4b0f7256ce868a10341d2c64f36cecf7780c024e7b0c8ddc292ddd",
        ),
        (b"", "8f40661c06ee078c83c534185b6a503938fdf41ab4f0a17e2ac9c109da2e2c92"),
    ],
)
def test_signature_matches_openssl_vectors(payload: bytes, expected: str) -> None:
    assert compute_signature(SECRET, T0, payload) == expected
    header = f"t={T0},v1={expected}"
    assert verify_signature(payload, header, [SECRET], now=T0) == T0


def test_dispute_event_types_match_vendored_spec() -> None:
    assert set(SPEC["x-scruple-dispute-event-types"]) == DISPUTE_EVENT_TYPES


def test_header_with_v0_and_several_v1_and_rotation() -> None:
    body = b"{}"
    good = compute_signature(SECRET, T0, body)
    header = f"t={T0},v1={'0' * 64},v1={good},v0={'f' * 64}"
    assert verify_signature(body, header, [SECRET], now=T0) == T0
    # Rotation: the receiver holds old and new secrets; a signature from either passes.
    rotated = sign_header(OTHER, T0, body)
    assert verify_signature(body, rotated, [SECRET, OTHER], now=T0) == T0
    with pytest.raises(SignatureError):
        verify_signature(body, rotated, [SECRET], now=T0)


# --- attacks --------------------------------------------------------------------


def test_tampered_payload_rejected() -> None:
    body = event_body()
    header = sign_header(SECRET, T0, body)
    with pytest.raises(SignatureError, match="matches"):
        verify_signature(body.replace(b"1000", b"9000"), header, [SECRET], now=T0)


def test_wrong_secret_rejected() -> None:
    body = event_body()
    with pytest.raises(SignatureError):
        verify_signature(body, sign_header(OTHER, T0, body), [SECRET], now=T0)


def test_timestamp_swap_rejected() -> None:
    """Re-labelling a valid signature with a fresh t= breaks it (t is signed)."""
    body = event_body()
    sig = compute_signature(SECRET, T0 - 10_000, body)
    with pytest.raises(SignatureError, match="matches"):
        verify_signature(body, f"t={T0},v1={sig}", [SECRET], now=T0)


@pytest.mark.parametrize(
    ("offset", "ok"),
    [(0, True), (300, True), (-300, True), (301, False), (-301, False), (86_400, False)],
)
def test_tolerance_window_both_directions(offset: int, ok: bool) -> None:
    body = b"{}"
    header = sign_header(SECRET, T0, body)
    if ok:
        verify_signature(body, header, [SECRET], now=T0 + offset)
    else:
        with pytest.raises(SignatureError, match="tolerance"):
            verify_signature(body, header, [SECRET], now=T0 + offset)


def test_replay_within_window_is_acknowledged_not_reprocessed() -> None:
    r = receiver()
    body = event_body()
    header = sign_header(SECRET, T0, body)
    first = r.handle(body, header)
    assert not first.duplicate
    assert first.batch is not None
    again = r.handle(body, header)
    assert again.duplicate
    assert again.batch is None
    assert len(r.received) == 1


def test_replay_after_window_rejected() -> None:
    body = event_body()
    header = sign_header(SECRET, T0, body)
    with pytest.raises(SignatureError):
        receiver(T0 + 3600).handle(body, header)


def test_failed_mapping_not_remembered() -> None:
    r = receiver()
    bad = event_body(data={"object": {"object": "dispute"}})
    with pytest.raises(SchemaMismatchError):
        r.handle(bad, sign_header(SECRET, T0, bad))
    good = event_body()
    assert not r.handle(good, sign_header(SECRET, T0, good)).duplicate


def test_dedupe_memory_is_bounded() -> None:
    r = receiver(remember=2)
    for i in range(3):
        b = event_body(id=f"evt_{i}")
        r.handle(b, sign_header(SECRET, T0, b))
    b0 = event_body(id="evt_0")
    assert not r.handle(b0, sign_header(SECRET, T0, b0)).duplicate
    assert len(r.received) == 2


def test_non_dispute_event_verified_but_not_ingested() -> None:
    body = json.dumps(RES["event"]).encode()  # the real fixture: plan.created
    res = receiver().handle(body, sign_header(SECRET, T0, body))
    assert res.event_type == "plan.created"
    assert res.batch is None


def test_live_event_refused() -> None:
    body = event_body(livemode=True)
    with pytest.raises(LiveModeRefusedError):
        receiver().handle(body, sign_header(SECRET, T0, body))


def test_oversized_body_rejected_before_hmac() -> None:
    body = b" " * 2000
    with pytest.raises(InputTooLargeError):
        receiver(max_body_bytes=1000).handle(body, sign_header(SECRET, T0, body))


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "garbage",
        "t=",
        f"v1={'a' * 64}",
        f"t={T0}",
        f"t={T0},v1=",
        f"t={T0},v1={'A' * 64}",
        f"t={T0},v1={'a' * 63}",
        f"t={T0},t={T0},v1={'a' * 64}",
        f"t=-5,v1={'a' * 64}",
        f"t=1e9,v1={'a' * 64}",
        f"t={T0},v1={'a' * 64},=x",
        f"t={T0}," + ",".join([f"v1={'a' * 64}"] * 9),
        "t=" + "9" * 5000,
        f"t={T0},v1={'é' * 64}",
    ],
)
def test_malformed_headers(header: str | None) -> None:
    with pytest.raises(SignatureError):
        verify_signature(b"{}", header, [SECRET], now=T0)


@pytest.mark.parametrize("secrets", [[], [""], ["sk_test_abc"], ["whsec_"]])
def test_bad_secret_configuration(secrets: list[str]) -> None:
    body = b"{}"
    with pytest.raises(SignatureError):
        verify_signature(body, sign_header(SECRET, T0, body), secrets, now=T0)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=200), st.binary(max_size=200))
def test_fuzz_verify_never_crashes(header: str, payload: bytes) -> None:
    with pytest.raises(SignatureError):
        verify_signature(payload, header, [SECRET], now=T0)


@settings(max_examples=100, deadline=None)
@given(st.binary(max_size=300))
def test_fuzz_signed_garbage_raises_typed(payload: bytes) -> None:
    with contextlib.suppress(IngestError):
        receiver().handle(payload, sign_header(SECRET, T0, payload))


def test_parse_header_returns_values() -> None:
    assert parse_signature_header(f"t=12, v1={'b' * 64}") == (12, ("b" * 64,))


# --- route ----------------------------------------------------------------------


def app_with(r: StripeWebhookReceiver | None) -> TestClient:
    app = FastAPI()
    app.include_router(ingest_router(r))
    return TestClient(app)


def post(c: TestClient, body: bytes, header: str | None) -> Any:
    headers = {"content-type": "application/json"}
    if header is not None:
        headers["Stripe-Signature"] = header
    return c.post("/ingest/stripe/webhook", content=body, headers=headers)


def test_route_accepts_signed_dispute_event() -> None:
    r = receiver()
    c = app_with(r)
    body = event_body()
    resp = post(c, body, sign_header(SECRET, T0, body))
    assert resp.status_code == 200
    assert resp.json() == {
        "received": True,
        "event_id": "evt_test_1",
        "duplicate": False,
        "cases": 1,
        "movements": 0,
    }
    assert post(c, body, sign_header(SECRET, T0, body)).json()["duplicate"] is True


def test_route_status_codes() -> None:
    c = app_with(receiver())
    body = event_body()
    assert post(c, body, None).status_code == 400
    assert post(c, body, sign_header(OTHER, T0, body)).status_code == 400
    bad = b'{"object": "event"}'
    assert post(c, bad, sign_header(SECRET, T0, bad)).status_code == 422
    big = b" " * (2 * 1024 * 1024 + 1)
    assert post(c, big, sign_header(SECRET, T0, big)).status_code == 413


def test_route_unconfigured_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SECRETS_ENV, raising=False)
    assert post(app_with(None), b"{}", "t=1,v1=" + "a" * 64).status_code == 503


def test_main_app_mounts_route_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRETS_ENV, f" {SECRET} , {OTHER} ")
    gate = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), GateConfig(n_cases=100))
    c = TestClient(create_app(gate))
    body = event_body()
    # The env-configured receiver uses the system clock, so sign with the current time.
    now = int(datetime.now(UTC).timestamp())
    resp = post(c, body, sign_header(OTHER, now, body))
    assert resp.status_code == 200
    assert resp.json()["cases"] == 1
