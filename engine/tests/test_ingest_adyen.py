"""Adyen dispute notifications: HMAC ported from Adyen's library and checked against
its own vectors, contract tests against the vendored Webhooks spec, mapping, fuzzing."""

from __future__ import annotations

import contextlib
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from scruple.ingest.adyen import (
    EVENT_MAP,
    SCHEMA_OF,
    STATUS_MAP,
    ingest_notification,
    item_signature,
    signing_string,
    verify_item,
    verify_payload_hmac,
)
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    LiveModeRefusedError,
    SchemaMismatchError,
    SignatureError,
)
from scruple.ingest.models import CaseState
from scruple.sim.models import Stage
from tests.fixtures.openapi_contract import check_model, load_schemas

FIX = Path(__file__).parent / "fixtures" / "adyen"
SPEC = json.loads((FIX / "webhooks-v1.subset.json").read_text())
SCHEMAS = load_schemas(FIX / "webhooks-v1.subset.json")
EXAMPLES: dict[str, Any] = SPEC["components"]["examples"]
# Keys published in Adyen's library tests (test/UtilTest.py), not real merchant keys.
LIB_KEY = "44782DEF547AAA06C910C43932B1EB0C71FC68D9D0C057550C48EC2ACF6BA056"
MOCK_KEY = "74F490DD33F7327BAECC88B2947C011FC02D014A473AAA33A8EC93E4DC069174"
TEST_KEY = "00" * 32
OTHER_KEY = "11" * 32


def example(name: str) -> dict[str, Any]:
    v: dict[str, Any] = copy.deepcopy(EXAMPLES[name]["value"])
    return v


def signed(env: dict[str, Any], key: str = TEST_KEY) -> dict[str, Any]:
    """Re-sign every item of a spec example (its published signature is a placeholder)."""
    for w in env["notificationItems"]:
        item = w["NotificationRequestItem"]
        sig = item_signature(item, key)
        if not isinstance(item.get("additionalData"), dict):
            item["additionalData"] = {}
        item["additionalData"]["hmacSignature"] = sig
    return env


CHARGEBACK = "post-CHARGEBACK-chargeback"


# --- HMAC: the library's own vectors ------------------------------------------


def test_library_item_vector() -> None:
    request = {
        "pspReference": "7914073381342284",
        "merchantReference": "TestPayment-1407325143704",
        "merchantAccountCode": "TestMerchant",
        "amount": {"currency": "EUR", "value": 1130},
        "eventCode": "AUTHORISATION",
        "success": "true",
        "eventDate": "2019-05-06T17:15:34.121+02:00",
        "operations": ["CANCEL", "CAPTURE", "REFUND"],
        "paymentMethod": "visa",
    }
    assert item_signature(request, LIB_KEY) == "coqCmt/IZ4E3CzPvMY8zTjQVL5hYJUiBRg8UU+iCWo0="
    assert signing_string(request) == (
        "7914073381342284::TestMerchant:TestPayment-1407325143704:1130:EUR:AUTHORISATION:true"
    )


@pytest.mark.parametrize(
    "name",
    [
        "backslash_notification.json",
        "colon_notification.json",
        "forwardslash_notification.json",
        "mixed_notification.json",
    ],
)
def test_library_mock_notifications_verify(name: str) -> None:
    item = json.loads((FIX / name).read_text())
    verify_item(item, [MOCK_KEY])
    with pytest.raises(SignatureError):
        verify_item(item, [OTHER_KEY])
    item["merchantReference"] += "x"
    with pytest.raises(SignatureError):
        verify_item(item, [MOCK_KEY])


def test_library_payload_vector() -> None:
    payload = (FIX / "hmac_payload_vector.txt").read_bytes()
    verify_payload_hmac(payload, "fX74xUdztFmaXAn3IusMFFUBUSkLmDQUK0tm8xL6ZTU=", [LIB_KEY])
    with pytest.raises(SignatureError):
        verify_payload_hmac(payload, "MismatchingHmacKey=", [LIB_KEY])
    with pytest.raises(SignatureError):
        verify_payload_hmac(
            payload + b" ", "fX74xUdztFmaXAn3IusMFFUBUSkLmDQUK0tm8xL6ZTU=", [LIB_KEY]
        )


@pytest.mark.parametrize("key", ["", "abc", "zz" * 32, "0" * 300])
def test_bad_keys(key: str) -> None:
    with pytest.raises(SignatureError):
        verify_item(
            signed(example(CHARGEBACK))["notificationItems"][0]["NotificationRequestItem"], [key]
        )


def test_key_rotation() -> None:
    env = signed(example(CHARGEBACK), OTHER_KEY)
    assert ingest_notification(env, [TEST_KEY, OTHER_KEY]).cases
    with pytest.raises(SignatureError):
        ingest_notification(env, [TEST_KEY])


# --- contract: vendored Adyen Webhooks spec ------------------------------------


@pytest.mark.parametrize(("model", "schema"), list(SCHEMA_OF.items()), ids=str)
def test_wire_models_match_spec(model: type[BaseModel], schema: str) -> None:
    assert check_model(model, schema, SCHEMAS, SCHEMA_OF) == []


def test_event_map_is_exactly_the_dispute_webhooks() -> None:
    assert set(EVENT_MAP) == set(SPEC["x-scruple-dispute-webhooks"])


def test_status_map_is_the_spec_enum() -> None:
    enum = SCHEMAS["DisputesNotificationAdditionalData"]["properties"]["disputeStatus"]["enum"]
    assert set(STATUS_MAP) == set(enum)


def test_signed_fields_exist_in_spec() -> None:
    props = SCHEMAS["DisputesNotificationRequestItem"]["properties"]
    for f in (
        "pspReference",
        "originalReference",
        "merchantAccountCode",
        "merchantReference",
        "eventCode",
        "success",
        "amount",
    ):
        assert f in props
    assert set(SCHEMAS["Amount"]["properties"]) == {"currency", "value"}


# --- mapping --------------------------------------------------------------------


def test_chargeback_example_maps() -> None:
    res = ingest_notification(signed(example(CHARGEBACK)), [TEST_KEY])
    (case,) = res.cases
    assert res.skipped == ()
    assert case.case_id == "adyen:QFQTPCQ8HXSKGK82"
    assert case.payment_ref == "9913140798220028"
    assert (case.stage, case.state) == (Stage.CHARGEBACK, CaseState.OPEN)
    assert (case.amount_minor, case.currency) == (1000, "EUR")
    assert case.network == "mc"
    assert case.network_reason_code == "4853"  # leading space in the example is stripped
    assert case.reason_code is None
    assert case.evidence_due_at is not None
    assert case.evidence_due_at.isoformat() == "2021-07-31T03:03:07+02:00"
    assert "Payment.TxId" not in case.model_dump_json()  # free-text reason is not kept


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_every_vendored_dispute_example_maps(name: str) -> None:
    env = signed(example(name))
    res = ingest_notification(env, [TEST_KEY], allow_live=env["live"] == "true")
    (case,) = res.cases
    code = env["notificationItems"][0]["NotificationRequestItem"]["eventCode"]
    stage, _ = EVENT_MAP[code]
    assert case.stage is stage
    assert case.source_reason == code


@pytest.mark.parametrize(("status", "state"), list(STATUS_MAP.items()))
def test_dispute_status_overrides_event_state(status: str, state: CaseState) -> None:
    env = example(CHARGEBACK)
    env["notificationItems"][0]["NotificationRequestItem"]["additionalData"]["disputeStatus"] = (
        status
    )
    assert ingest_notification(signed(env), [TEST_KEY]).cases[0].state is state


def test_non_dispute_and_failed_items_are_reported_not_dropped() -> None:
    env = example(CHARGEBACK)
    item = env["notificationItems"][0]["NotificationRequestItem"]
    auth = dict(copy.deepcopy(item), eventCode="AUTHORISATION", pspReference="P2")
    failed = dict(copy.deepcopy(item), success="false", pspReference="P3")
    env["notificationItems"] += [
        {"NotificationRequestItem": auth},
        {"NotificationRequestItem": failed},
    ]
    res = ingest_notification(signed(env), [TEST_KEY])
    assert len(res.cases) == 1
    assert [(s.index, s.reason) for s in res.skipped] == [
        (1, "not a dispute event"),
        (2, "success is false"),
    ]


def test_one_forged_item_rejects_the_delivery() -> None:
    env = signed(example(CHARGEBACK))
    forged = copy.deepcopy(env["notificationItems"][0])
    forged["NotificationRequestItem"]["amount"]["value"] = 999_999
    env["notificationItems"].append(forged)
    with pytest.raises(SignatureError):
        ingest_notification(env, [TEST_KEY])


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("amount", "value"), "1000"),
        (("amount", "value"), 10.5),
        (("amount", "value"), -1),
        (("amount", "currency"), "EURO"),
        (("amount", "currency"), "zzz"),
        (("eventDate",), "yesterday"),
        (("eventDate",), "2021-01-01T01:00:00"),
        (("success",), True),
        (("pspReference",), None),
    ],
)
def test_strict_fields(path: tuple[str, ...], value: object) -> None:
    env = example(CHARGEBACK)
    cur = env["notificationItems"][0]["NotificationRequestItem"]
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value
    with pytest.raises(IngestError):
        ingest_notification(env, [TEST_KEY], verify=False)


def test_envelope_rules() -> None:
    env = signed(example(CHARGEBACK))
    with pytest.raises(LiveModeRefusedError):
        ingest_notification(dict(env, live="true"), [TEST_KEY])
    assert ingest_notification(dict(env, live="true"), [TEST_KEY], allow_live=True).cases
    bads: list[dict[str, Any]] = [
        {"live": False},
        {"live": "false"},
        {"live": "false", "notificationItems": []},
        {"live": "false", "notificationItems": [1]},
    ]
    for bad in bads:
        with pytest.raises(SchemaMismatchError):
            ingest_notification(bad, [TEST_KEY])
    many = dict(env, notificationItems=env["notificationItems"] * 101)
    with pytest.raises(InputTooLargeError):
        ingest_notification(many, [TEST_KEY])
    item = env["notificationItems"][0]["NotificationRequestItem"]
    del item["additionalData"]["hmacSignature"]
    with pytest.raises(SignatureError):
        ingest_notification(env, [TEST_KEY])
    with pytest.raises(SignatureError):
        ingest_notification(signed(example(CHARGEBACK)), [])


def test_json_bytes() -> None:
    raw = json.dumps(signed(example(CHARGEBACK))).encode()
    assert ingest_notification(raw, [TEST_KEY]).cases


# --- fuzzing --------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(st.binary(max_size=300))
def test_fuzz_bytes(data: bytes) -> None:
    with contextlib.suppress(IngestError):
        ingest_notification(data, [TEST_KEY])


leaf = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text(max_size=12)
)
FIELDS = [
    "pspReference",
    "originalReference",
    "merchantAccountCode",
    "merchantReference",
    "eventCode",
    "eventDate",
    "success",
    "amount",
    "additionalData",
]


@settings(max_examples=300, deadline=None)
@given(
    st.sampled_from(FIELDS), st.one_of(leaf, st.dictionaries(st.text(max_size=8), leaf, max_size=3))
)
def test_fuzz_fields_signed_and_unsigned(field: str, value: Any) -> None:
    env = example(CHARGEBACK)
    env["notificationItems"][0]["NotificationRequestItem"][field] = value
    for verify in (True, False):
        with contextlib.suppress(IngestError):
            ingest_notification(copy.deepcopy(env), [TEST_KEY], verify=verify)
    with contextlib.suppress(IngestError):
        ingest_notification(signed(env), [TEST_KEY])  # item_signature may raise IngestError
