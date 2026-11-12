"""POST /ingest/adyen/webhook over HTTP: real HMAC, Adyen's own vectors, limits.

The vectors are the ones Adyen publishes in its Python library's tests
(tests/fixtures/adyen/README.md): the item from `UtilTest.test_hmac_signature` and
the mock notifications of `test_webhooks_with_slashes`, each with its own key. They
are AUTHORISATION items, not dispute events, so a delivery made of them verifies,
is accepted and is reported as skipped. Dispute deliveries are signed here with the
same HMAC code under a test key, then checked through the full app.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.limits import Limits
from scruple.auth.ratelimit import InMemoryBuckets, Limit
from scruple.auth.rbac import key_permissions
from scruple.ingest.adyen import item_signature
from scruple.ingest.api import ADYEN_KEYS_ENV, CASE_STORE_ENV, CASE_STORES_ENV, TENANT_ENV
from scruple.ingest.limits import MAX_JSON_BYTES
from scruple.ingest.store import CaseStore
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage
from tests.test_ingest_adyen import LIB_KEY, MOCK_KEY, OTHER_KEY, TEST_KEY, signed
from tests.test_ingest_webhook_store import adyen_ack, adyen_body

FIX = Path(__file__).parent / "fixtures" / "adyen"
URL = "/ingest/adyen/webhook"
FAST = ScryptParams(n=2**4)

# Adyen's library item vector (test/UtilTest.py): this key signs this item to this value.
LIB_ITEM: dict[str, Any] = {
    "pspReference": "7914073381342284",
    "merchantReference": "TestPayment-1407325143704",
    "merchantAccountCode": "TestMerchant",
    "amount": {"currency": "EUR", "value": 1130},
    "eventCode": "AUTHORISATION",
    "success": "true",
    "eventDate": "2019-05-06T17:15:34.121+02:00",
    "operations": ["CANCEL", "CAPTURE", "REFUND"],
    "paymentMethod": "visa",
    "additionalData": {"hmacSignature": "coqCmt/IZ4E3CzPvMY8zTjQVL5hYJUiBRg8UU+iCWo0="},
}
MOCKS = (
    "backslash_notification.json",
    "colon_notification.json",
    "forwardslash_notification.json",
    "mixed_notification.json",
)


def envelope(*items: dict[str, Any]) -> dict[str, Any]:
    return {"live": "false", "notificationItems": [{"NotificationRequestItem": i} for i in items]}


def mock(name: str) -> dict[str, Any]:
    item: dict[str, Any] = json.loads((FIX / name).read_text())
    return item


@pytest.fixture
def make(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """A full app (auth middleware, limits) with the given Adyen keys and a case store."""
    opened: list[Any] = []
    for env in (CASE_STORES_ENV, TENANT_ENV):
        monkeypatch.delenv(env, raising=False)

    def build(keys: str, *, burst: int = 100, store: bool = True) -> tuple[TestClient, Any]:
        monkeypatch.setenv(ADYEN_KEYS_ENV, keys)
        path = tmp_path / f"cases{len(opened)}.db"
        if store:
            monkeypatch.setenv(CASE_STORE_ENV, str(path))
        else:
            monkeypatch.delenv(CASE_STORE_ENV, raising=False)
        db = SQLiteStorage(":memory:")
        opened.append(db)
        limits = Limits(
            anonymous=Limit(per_minute=60, burst=burst),
            backend=InMemoryBuckets(),
        )
        svc = ApiKeyService(db, params=FAST, permissions_for=key_permissions)
        app = create_app(AgentGate(db), auth=Authenticator(svc), limits=limits)
        return TestClient(app), path

    yield build
    for db in opened:
        db.close()


def test_library_item_vector_is_accepted_over_http(make: Any) -> None:
    c, _ = make(LIB_KEY, store=False)
    assert item_signature(LIB_ITEM, LIB_KEY) == LIB_ITEM["additionalData"]["hmacSignature"]
    ack = adyen_ack(c.post(URL, json=envelope(LIB_ITEM)))
    assert ack == {"cases": 0, "skipped": 1, "duplicates": 0, "stale": 0, "stored": False}


def test_library_item_vector_tampered_is_rejected(make: Any) -> None:
    c, _ = make(LIB_KEY, store=False)
    item = copy.deepcopy(LIB_ITEM)
    item["amount"]["value"] = 1131
    r = c.post(URL, json=envelope(item))
    assert r.status_code == 400 and "signature" in r.json()["detail"]
    assert r.text != "[accepted]"


def test_library_mock_notifications_are_accepted_together(make: Any) -> None:
    c, _ = make(MOCK_KEY, store=False)
    ack = adyen_ack(c.post(URL, json=envelope(*(mock(n) for n in MOCKS))))
    assert ack["skipped"] == len(MOCKS) and ack["cases"] == 0


def test_one_bad_library_item_rejects_the_batch(make: Any) -> None:
    c, _ = make(MOCK_KEY, store=False)
    items = [mock(n) for n in MOCKS]
    items[2]["merchantReference"] += "x"
    assert c.post(URL, json=envelope(*items)).status_code == 400


def test_wrong_key_rejects_library_vector(make: Any) -> None:
    c, _ = make(OTHER_KEY, store=False)
    assert c.post(URL, json=envelope(LIB_ITEM)).status_code == 400


def test_dispute_is_stored_once_through_the_full_app(make: Any) -> None:
    c, path = make(TEST_KEY)
    body = adyen_body()
    first = adyen_ack(c.post(URL, json=body))
    assert first == {"cases": 1, "skipped": 0, "duplicates": 0, "stale": 0, "stored": True}
    again = adyen_ack(c.post(URL, json=body))
    assert again["duplicates"] == 1
    store = CaseStore(path, create=False)
    try:
        assert len(store.cases()) == 1 and len(store.webhook_events()) == 1
    finally:
        store.close()


def test_forged_dispute_in_a_batch_stores_nothing(make: Any) -> None:
    c, path = make(TEST_KEY)
    good = adyen_body()
    forged = signed(adyen_body(pspReference="9915555555555555"), OTHER_KEY)
    batch = {**good, "notificationItems": good["notificationItems"] + forged["notificationItems"]}
    assert c.post(URL, json=batch).status_code == 400
    store = CaseStore(path, create=False)
    try:
        assert store.cases() == [] and store.webhook_events() == []
    finally:
        store.close()


def test_item_without_signature_is_rejected(make: Any) -> None:
    c, _ = make(TEST_KEY, store=False)
    item = adyen_body()
    item["notificationItems"][0]["NotificationRequestItem"]["additionalData"].pop("hmacSignature")
    assert c.post(URL, json=item).status_code == 400


def test_body_size_limit(make: Any) -> None:
    c, _ = make(TEST_KEY, store=False)
    big = b" " * (MAX_JSON_BYTES + 1)
    r = c.post(URL, content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413

    def chunks() -> Iterator[bytes]:  # no Content-Length: refused while streaming
        for _ in range(MAX_JSON_BYTES // 65536 + 2):
            yield b" " * 65536

    r = c.post(URL, content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_rate_limited_per_client_address(make: Any) -> None:
    c, _ = make(TEST_KEY, burst=3, store=False)
    body = adyen_body()
    codes = [c.post(URL, json=body).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = c.post(URL, json=body)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_unconfigured_is_503(make: Any) -> None:
    c, _ = make("", store=False)
    assert c.post(URL, json=adyen_body()).status_code == 503
