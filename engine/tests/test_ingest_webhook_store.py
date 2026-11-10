"""Accepted Stripe and Adyen webhook deliveries are persisted into the case store."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.ingest.api import ADYEN_KEYS_ENV, TENANT_ENV, ingest_router
from scruple.ingest.models import CaseState
from scruple.ingest.store import CaseStore, StoreError
from scruple.ingest.stripe.webhook import StripeWebhookReceiver, sign_header
from tests.test_ingest_adyen import CHARGEBACK, TEST_KEY, example, signed
from tests.test_ingest_stripe_webhook import RES, SECRET, T0, FixedClock

DISPUTE: dict[str, Any] = RES["dispute"]


def stripe_event(evt: str, created: int, status: str) -> bytes:
    ev: dict[str, Any] = copy.deepcopy(RES["event"])
    d = copy.deepcopy(DISPUTE)
    d["status"] = status
    ev.update(type="charge.dispute.updated", id=evt, created=created, data={"object": d})
    return json.dumps(ev).encode()


def client(store: CaseStore, **kw: Any) -> TestClient:
    app = FastAPI()
    receiver = StripeWebhookReceiver([SECRET], clock=FixedClock(T0))
    app.include_router(ingest_router(receiver, store=store, **kw))
    return TestClient(app)


def post_stripe(c: TestClient, body: bytes) -> Any:
    return c.post(
        "/ingest/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": sign_header(SECRET, T0, body)},
    )


def current_state(store: CaseStore) -> dict[str, str]:
    return {sc.case.case_id: sc.case.state.value for sc in store.cases()}


@pytest.fixture
def store(tmp_path: Path) -> CaseStore:
    return CaseStore(tmp_path / "cases.db")


# --- Stripe --------------------------------------------------------------------------


def test_accepted_stripe_batch_is_stored(store: CaseStore) -> None:
    r = post_stripe(client(store), stripe_event("evt_a", T0 - 10, "warning_needs_response"))
    assert r.status_code == 200, r.text
    assert r.json()["stored"] and r.json()["cases"] == 1 and not r.json()["duplicate"]
    [sc] = store.cases()
    assert sc.case.case_id == f"stripe:{DISPUTE['id']}"
    events = store.webhook_events()
    assert [(e["source"], e["event_id"], e["run_id"]) for e in events] == [
        ("stripe", "evt_a", store.runs()[0]["run_id"])
    ]


def test_redelivery_after_a_restart_is_a_duplicate(store: CaseStore) -> None:
    body = stripe_event("evt_a", T0 - 10, "warning_needs_response")
    assert not post_stripe(client(store), body).json()["duplicate"]
    # A new process: the receiver's in-memory id cache is empty, the store is not.
    again = post_stripe(client(store), body)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert len(store.runs()) == 1 and len(store.webhook_events()) == 1


def test_out_of_order_delivery_keeps_the_newest_state(store: CaseStore) -> None:
    c = client(store)
    post_stripe(c, stripe_event("evt_new", T0 - 10, "won"))
    late = post_stripe(c, stripe_event("evt_old", T0 - 100, "warning_needs_response"))
    assert late.status_code == 200 and late.json()["stale"] == 1
    assert current_state(store) == {f"stripe:{DISPUTE['id']}": CaseState.WON.value}
    applied = {e["event_id"]: e["applied"] for e in store.webhook_case_events()}
    assert applied == {"evt_new": True, "evt_old": False}  # the late one is kept, not applied


def test_a_failed_store_write_is_retried_by_the_next_delivery(
    store: CaseStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = client(store)
    body = stripe_event("evt_a", T0 - 10, "lost")
    real = store.write_webhook

    def broken(**kw: Any) -> Any:
        raise StoreError("disk full")

    monkeypatch.setattr(store, "write_webhook", broken)
    assert post_stripe(c, body).status_code == 503  # the provider will redeliver
    monkeypatch.setattr(store, "write_webhook", real)
    retry = post_stripe(c, body)  # same receiver: the id was not remembered
    assert retry.status_code == 200 and not retry.json()["duplicate"] and retry.json()["stored"]
    assert current_state(store) == {f"stripe:{DISPUTE['id']}": CaseState.LOST.value}


def test_without_a_store_nothing_is_stored(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(ingest_router(StripeWebhookReceiver([SECRET], clock=FixedClock(T0))))
    r = post_stripe(TestClient(app), stripe_event("evt_a", T0, "won"))
    assert r.status_code == 200 and r.json()["stored"] is False


STATUSES = ["warning_needs_response", "needs_response", "under_review", "won", "lost"]


@settings(max_examples=25, deadline=None)
@given(
    order=st.permutations(range(5)),
    dupes=st.lists(st.integers(0, 4), max_size=4),
)
def test_any_delivery_order_with_duplicates_ends_in_the_same_state(
    order: list[int], dupes: list[int]
) -> None:
    """Five events for one dispute, delivered in any order, some twice: the stored
    current state is always the newest event's, each event is stored once."""
    store = CaseStore(":memory:")
    c = client(store)
    bodies = [stripe_event(f"evt_{i}", T0 - 500 + 100 * i, STATUSES[i]) for i in range(5)]
    for i in [*order, *dupes]:
        assert post_stripe(c, bodies[i]).status_code == 200
    final = store.cases()[0].case
    assert final.state is CaseState.LOST  # STATUSES[4], the newest
    assert sorted(e["event_id"] for e in store.webhook_events()) == [f"evt_{i}" for i in range(5)]
    assert sum(e["applied"] for e in store.webhook_case_events()) >= 1


# --- Adyen ---------------------------------------------------------------------------


def adyen_client(store: CaseStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(ADYEN_KEYS_ENV, TEST_KEY)
    app = FastAPI()
    app.include_router(ingest_router(store=store))
    return TestClient(app)


def adyen_body(**item: Any) -> dict[str, Any]:
    env = example(CHARGEBACK)
    env["notificationItems"][0]["NotificationRequestItem"].update(item)
    return signed(env)


def test_adyen_notification_is_stored_and_deduplicated(
    store: CaseStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = adyen_client(store, monkeypatch)
    body = adyen_body()
    r = c.post("/ingest/adyen/webhook", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["cases"] == 1 and r.json()["duplicates"] == 0 and r.json()["stored"]
    again = c.post("/ingest/adyen/webhook", json=body)
    assert again.json()["duplicates"] == 1 and len(store.runs()) == 1


def test_adyen_late_event_does_not_overwrite_a_newer_one(
    store: CaseStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = adyen_client(store, monkeypatch)
    newer = adyen_body(eventCode="CHARGEBACK_REVERSED", eventDate="2030-01-02T00:00:00+00:00")
    older = adyen_body(eventDate="2030-01-01T00:00:00+00:00")
    assert c.post("/ingest/adyen/webhook", json=newer).json()["stale"] == 0
    assert c.post("/ingest/adyen/webhook", json=older).json()["stale"] == 1
    [sc] = store.cases()
    assert sc.case.state is CaseState.WON and sc.case.source_reason == "CHARGEBACK_REVERSED"


def test_adyen_bad_signature_stores_nothing(
    store: CaseStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = adyen_client(store, monkeypatch)
    body = adyen_body()
    body["notificationItems"][0]["NotificationRequestItem"]["amount"]["value"] += 1
    assert c.post("/ingest/adyen/webhook", json=body).status_code == 400
    assert store.cases() == [] and store.webhook_events() == []


def test_adyen_without_keys_is_503(store: CaseStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ADYEN_KEYS_ENV, raising=False)
    app = FastAPI()
    app.include_router(ingest_router(store=store))
    assert TestClient(app).post("/ingest/adyen/webhook", json=adyen_body()).status_code == 503


# --- the store API directly ----------------------------------------------------------


def test_webhook_tables_are_append_only(store: CaseStore) -> None:
    post_stripe(client(store), stripe_event("evt_a", T0, "won"))
    import sqlite3

    for sql in ("DELETE FROM webhook_events", "UPDATE webhook_case_events SET applied = 0"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store._db.execute(sql)


def test_ingest_tenant_comes_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhooks carry no API key, so the tenant they write for is configuration."""
    monkeypatch.setenv(TENANT_ENV, "Not A Tenant")
    with pytest.raises(ValueError, match=TENANT_ENV):
        ingest_router()
    monkeypatch.setenv(TENANT_ENV, "bank-a")
    assert ingest_router().routes  # a valid tenant id is accepted
