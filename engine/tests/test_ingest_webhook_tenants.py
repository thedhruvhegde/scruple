"""Webhook deliveries are routed to a tenant by the secret that signed them.

Each tenant has its own case store; a delivery signed with one tenant's secret is
written, and deduplicated, only in that tenant's store.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.ingest.api import (
    ADYEN_KEYS_ENV,
    CASE_STORE_ENV,
    CASE_STORES_ENV,
    SECRETS_ENV,
    TENANT_ENV,
    by_tenant,
    ingest_router,
)
from scruple.ingest.errors import SignatureError
from scruple.ingest.store import CaseStore
from scruple.ingest.stripe.webhook import (
    StripeWebhookReceiver,
    compute_signature,
    sign_header,
    signing_tenant,
)
from tests.test_ingest_adyen import OTHER_KEY, TEST_KEY, signed
from tests.test_ingest_stripe_webhook import OTHER, SECRET, T0, FixedClock
from tests.test_ingest_webhook_store import adyen_body, stripe_event

A, B = "bank-a", "bank-b"
SECRETS = {A: SECRET, B: OTHER}
KEYS = {A: TEST_KEY, B: OTHER_KEY}


@pytest.fixture
def stores(tmp_path: Path) -> dict[str, CaseStore]:
    return {A: CaseStore(tmp_path / "a.db"), B: CaseStore(tmp_path / "b.db")}


def app_for(stores: dict[str, CaseStore], **kw: Any) -> TestClient:
    app = FastAPI()
    receivers = {t: StripeWebhookReceiver([s], clock=FixedClock(T0)) for t, s in SECRETS.items()}
    kw.setdefault("stripe_by_tenant", receivers)
    kw.setdefault("adyen_keys_by_tenant", {t: [k] for t, k in KEYS.items()})
    app.include_router(ingest_router(stores=stores, **kw))
    return TestClient(app)


def post(c: TestClient, body: bytes, secret: str, t: int = T0) -> Any:
    return c.post(
        "/ingest/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": sign_header(secret, t, body)},
    )


def events(store: CaseStore) -> list[tuple[str, str]]:
    return [(e["tenant_id"], e["event_id"]) for e in store.webhook_events()]


# --- parsing the configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([SECRET], {"default": [SECRET]}),
        ([f"{A}={SECRET}"], {A: [SECRET]}),
        (
            [f"{A}={SECRET}", f"{B}={OTHER}", SECRET + "x"],
            {A: [SECRET], B: [OTHER], "default": [SECRET + "x"]},
        ),
        ([f"{A}={SECRET}", f"{A}={OTHER}"], {A: [SECRET, OTHER]}),  # rotation
        ([f"{A}={SECRET}", f"{A}={SECRET}"], {A: [SECRET]}),
        ([TEST_KEY, f"{B}={OTHER_KEY}"], {"default": [TEST_KEY], B: [OTHER_KEY]}),
    ],
)
def test_entries_map_secrets_to_tenants(entries: list[str], expected: dict[str, list[str]]) -> None:
    assert by_tenant(entries, "default", "test") == expected


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        ([f"{A}={SECRET}", f"{B}={SECRET}"], "two tenants"),
        ([SECRET, f"{B}={SECRET}"], "two tenants"),
        ([f"Bank A={SECRET}"], "invalid tenant"),
        ([f"{A}="], "empty secret"),
    ],
)
def test_bad_mappings_are_refused(entries: list[str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        by_tenant(entries, "default", "test")


def test_signing_tenant_needs_exactly_one_match() -> None:
    body = b"{}"
    secrets = {A: [SECRET], B: [OTHER]}
    assert signing_tenant(body, sign_header(OTHER, T0, body), secrets) == B
    with pytest.raises(SignatureError, match="no v1"):
        signing_tenant(body, sign_header("whsec_nobody_0123456789", T0, body), secrets)
    both = (
        f"t={T0},v1={compute_signature(SECRET, T0, body)},v1={compute_signature(OTHER, T0, body)}"
    )
    with pytest.raises(SignatureError, match="more than one tenant"):
        signing_tenant(body, both, secrets)


# --- Stripe ------------------------------------------------------------------------------


def test_stripe_delivery_lands_in_the_signing_tenants_store(stores: dict[str, CaseStore]) -> None:
    c = app_for(stores)
    r = post(c, stripe_event("evt_a", T0 - 10, "won"), SECRET)
    assert r.status_code == 200 and r.json()["stored"], r.text
    assert events(stores[A]) == [(A, "evt_a")] and len(stores[A].cases()) == 1
    assert events(stores[B]) == [] and stores[B].cases() == []
    r = post(c, stripe_event("evt_b", T0 - 10, "lost"), OTHER)
    assert r.status_code == 200 and r.json()["stored"]
    assert events(stores[B]) == [(B, "evt_b")] and events(stores[A]) == [(A, "evt_a")]


def test_the_same_event_id_is_not_a_duplicate_across_tenants(
    stores: dict[str, CaseStore],
) -> None:
    c = app_for(stores)
    body = stripe_event("evt_same", T0 - 10, "won")
    assert not post(c, body, SECRET).json()["duplicate"]
    assert not post(c, body, OTHER).json()["duplicate"]  # another tenant's endpoint
    assert post(c, body, OTHER).json()["duplicate"]  # B's own redelivery
    assert events(stores[A]) == [(A, "evt_same")] and events(stores[B]) == [(B, "evt_same")]


def test_an_unknown_or_double_signature_stores_nothing(stores: dict[str, CaseStore]) -> None:
    c = app_for(stores)
    body = stripe_event("evt_x", T0 - 10, "won")
    assert post(c, body, "whsec_nobody_0123456789").status_code == 400
    both = (
        f"t={T0},v1={compute_signature(SECRET, T0, body)},v1={compute_signature(OTHER, T0, body)}"
    )
    r = c.post("/ingest/stripe/webhook", content=body, headers={"Stripe-Signature": both})
    assert r.status_code == 400 and "more than one tenant" in r.text
    assert post(c, body, SECRET, t=T0 - 10_000).status_code == 400  # stale, after routing
    assert all(s.webhook_events() == [] for s in stores.values())


@settings(max_examples=25, deadline=None)
@given(st.lists(st.tuples(st.sampled_from([A, B]), st.integers(0, 5)), max_size=12))
def test_property_each_store_holds_only_its_tenants_deliveries(
    deliveries: list[tuple[str, int]],
) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        stores = {A: CaseStore(Path(d) / "a.db"), B: CaseStore(Path(d) / "b.db")}
        c = app_for(stores)
        for tenant, n in deliveries:
            body = stripe_event(f"evt_{tenant}_{n}", T0 - 100 + n, "won" if n % 2 else "lost")
            assert post(c, body, SECRETS[tenant]).status_code == 200
        for tenant, s in stores.items():
            want = {(tenant, f"evt_{tenant}_{n}") for t, n in deliveries if t == tenant}
            assert set(events(s)) == want


# --- Adyen -------------------------------------------------------------------------------


def test_adyen_delivery_lands_in_the_verifying_tenants_store(
    stores: dict[str, CaseStore],
) -> None:
    c = app_for(stores)
    a = signed(adyen_body(), TEST_KEY)
    b = signed(adyen_body(pspReference="9915555555555555"), OTHER_KEY)
    assert c.post("/ingest/adyen/webhook", json=a).json()["stored"]
    assert c.post("/ingest/adyen/webhook", json=b).json()["stored"]
    assert {t for t, _ in events(stores[A])} == {A} and len(events(stores[A])) == 1
    assert {t for t, _ in events(stores[B])} == {B} and len(events(stores[B])) == 1
    # A's notification redelivered is a duplicate only in A's store.
    assert c.post("/ingest/adyen/webhook", json=a).json()["duplicates"] == 1
    assert len(stores[B].webhook_events()) == 1


def test_adyen_items_signed_by_two_tenants_are_rejected(stores: dict[str, CaseStore]) -> None:
    c = app_for(stores)
    a = signed(adyen_body(), TEST_KEY)
    b = signed(adyen_body(pspReference="9915555555555555"), OTHER_KEY)
    mixed = {**a, "notificationItems": a["notificationItems"] + b["notificationItems"]}
    assert c.post("/ingest/adyen/webhook", json=mixed).status_code == 400
    assert all(s.webhook_events() == [] for s in stores.values())


# --- configuration errors and the environment ----------------------------------------


def test_a_tenant_without_a_store_refuses_to_start(stores: dict[str, CaseStore]) -> None:
    with pytest.raises(ValueError, match="bank-b have webhook secrets but no case store"):
        app_for({A: stores[A]})


def test_two_tenants_cannot_share_a_store(tmp_path: Path) -> None:
    one = CaseStore(tmp_path / "x.db")
    with pytest.raises(ValueError, match="share one case store"):
        app_for({A: one, B: CaseStore(tmp_path / "x.db")})


def test_a_secret_shared_by_two_tenants_refuses_to_start(stores: dict[str, CaseStore]) -> None:
    same = {t: StripeWebhookReceiver([SECRET]) for t in (A, B)}
    with pytest.raises(ValueError, match="two tenants"):
        app_for(stores, stripe_by_tenant=same)


def test_mapping_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TENANT_ENV, A)
    monkeypatch.setenv(SECRETS_ENV, f"{SECRET},{B}={OTHER}")  # bare: the configured tenant
    monkeypatch.setenv(ADYEN_KEYS_ENV, f"{TEST_KEY},{B}={OTHER_KEY}")
    monkeypatch.setenv(CASE_STORE_ENV, str(tmp_path / "a.db"))
    monkeypatch.setenv(CASE_STORES_ENV, f"{B}={tmp_path / 'b.db'}")
    app = FastAPI()
    app.include_router(ingest_router())
    c = TestClient(app)
    now = int(time.time())
    assert post(c, stripe_event("evt_env_b", now - 5, "won"), OTHER, t=now).json()["stored"]
    assert post(c, stripe_event("evt_env_a", now - 5, "won"), SECRET, t=now).json()["stored"]
    assert events(CaseStore(tmp_path / "a.db")) == [(A, "evt_env_a")]
    assert events(CaseStore(tmp_path / "b.db")) == [(B, "evt_env_b")]


def test_environment_with_two_tenants_and_one_store_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CASE_STORES_ENV, raising=False)
    monkeypatch.delenv(ADYEN_KEYS_ENV, raising=False)
    monkeypatch.setenv(SECRETS_ENV, f"{A}={SECRET},{B}={OTHER}")
    monkeypatch.setenv(TENANT_ENV, A)
    monkeypatch.setenv(CASE_STORE_ENV, str(tmp_path / "a.db"))
    with pytest.raises(ValueError, match="no case store"):
        ingest_router()


def test_single_tenant_configuration_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ADYEN_KEYS_ENV, raising=False)
    store = CaseStore(tmp_path / "one.db")
    app = FastAPI()
    receiver = StripeWebhookReceiver([SECRET], clock=FixedClock(T0))
    app.include_router(ingest_router(receiver, store=store, tenant_id=B))
    r = post(TestClient(app), stripe_event("evt_one", T0 - 10, "won"), SECRET)
    assert r.json()["stored"] and events(store) == [(B, "evt_one")]
