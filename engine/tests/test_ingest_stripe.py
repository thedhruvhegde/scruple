"""Stripe disputes adapter: contract tests against the vendored real schema, mapping,
fuzzing and the HTTP client (mock transport only; never a live call)."""

from __future__ import annotations

import contextlib
import copy
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, StrictInt

from scruple.domain import ReasonCode
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    LiveModeRefusedError,
    MalformedInputError,
    SchemaMismatchError,
    UnmappedValueError,
)
from scruple.ingest.limits import load_json
from scruple.ingest.models import CaseState, MovementKind
from scruple.ingest.stripe import StripeAPIError, StripeClient, ingest_dispute
from scruple.ingest.stripe.mapping import EVIDENCE_FIELDS, REASON_MAP, STATUS_MAP
from scruple.ingest.stripe.wire import SCHEMA_OF, BalanceTransaction, Charge, Event
from scruple.sim.ledger import Ledger
from scruple.sim.models import Stage
from tests.fixtures.openapi_contract import check_model, load_schemas

HERE = Path(__file__).parent / "fixtures" / "stripe"
SPEC = json.loads((HERE / "spec3.subset.json").read_text())
SCHEMAS = load_schemas(HERE / "spec3.subset.json")
FIXTURES: dict[str, Any] = json.loads((HERE / "fixtures3.subset.json").read_text())["resources"]
KEY = "sk_test_" + "0" * 24  # dummy, never a real key


def dispute(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = copy.deepcopy(FIXTURES["dispute"])
    d.update(over)
    return d


def withdrawal(amount: int = -1000, fee: int = 1500, bt_id: str = "txn_w") -> dict[str, Any]:
    bt: dict[str, Any] = copy.deepcopy(FIXTURES["balance_transaction"])
    bt.update(
        id=bt_id,
        amount=amount,
        fee=fee,
        net=amount - fee,
        reporting_category="dispute",
        type="adjustment",
        fee_details=[{"amount": fee, "currency": "usd", "type": "stripe_fee"}] if fee else [],
    )
    return bt


# --- contract: our models against the real schema ---------------------------------


@pytest.mark.parametrize(("model", "schema"), list(SCHEMA_OF.items()), ids=lambda x: str(x))
def test_wire_model_matches_vendored_spec(model: type[BaseModel], schema: str) -> None:
    assert check_model(model, schema, SCHEMAS, SCHEMA_OF) == []


def test_contract_checker_catches_drift() -> None:
    class Wrong(BaseModel):
        amount: str  # spec says integer
        currency: str
        not_a_field: StrictInt  # not in spec

    class TooStrict(BaseModel):
        due_by: int  # spec: nullable and optional
        has_evidence: bool
        past_due: bool
        submission_count: int

    assert len(check_model(Wrong, "fee", SCHEMAS, SCHEMA_OF)) == 2
    assert len(check_model(TooStrict, "dispute_evidence_details", SCHEMAS, SCHEMA_OF)) == 2


def test_status_map_covers_spec_enum_exactly() -> None:
    enum = SCHEMAS["dispute"]["properties"]["status"]["enum"]
    assert set(STATUS_MAP) == set(enum)


def test_reason_map_covers_documented_reasons_exactly() -> None:
    desc = SCHEMAS["dispute"]["properties"]["reason"]["description"]
    documented = set(re.findall(r"`([a-z_]+)`", desc.split("Learn more")[0]))
    assert len(documented) == 15
    assert set(REASON_MAP) == documented


def test_evidence_fields_are_the_spec_fields_and_nullable() -> None:
    props = SCHEMAS["dispute_evidence"]["properties"]
    assert set(EVIDENCE_FIELDS) == set(props) - {"enhanced_evidence"}
    for f in EVIDENCE_FIELDS:
        assert props[f].get("nullable") is True, f


def test_dispute_event_types_vendored() -> None:
    assert "charge.dispute.created" in SPEC["x-scruple-dispute-event-types"]
    assert "charge.dispute.closed" in SPEC["x-scruple-dispute-event-types"]


@pytest.mark.parametrize("model", [BalanceTransaction, Charge, Event])
def test_real_fixture_objects_parse(model: type[BaseModel]) -> None:
    name = {BalanceTransaction: "balance_transaction", Charge: "charge", Event: "event"}[model]
    model.model_validate(FIXTURES[name])


# --- mapping -------------------------------------------------------------------


def test_real_fixture_dispute_maps() -> None:
    batch = ingest_dispute(FIXTURES["dispute"])
    (case,) = batch.cases
    assert case.case_id == "stripe:dp_1Pgc71B7WZ01zgkWMevJiAUx"
    assert case.payment_ref == "ch_1PgafuB7WZ01zgkWXYmPNZs8"
    assert (case.stage, case.state) == (Stage.INQUIRY, CaseState.OPEN)
    assert case.currency == "USD"
    assert case.amount_minor == 1000
    assert case.network == "visa"
    assert case.network_reason_code == "10.4"
    assert case.reason_code is None
    assert case.source_reason == "general"
    assert case.evidence_due_at is not None
    assert case.evidence_due_at.timestamp() == 1723679999
    assert batch.movements == ()


@pytest.mark.parametrize(("status", "expected"), list(STATUS_MAP.items()))
def test_every_status_maps(status: str, expected: tuple[Stage, CaseState]) -> None:
    (case,) = ingest_dispute(dispute(status=status)).cases
    assert (case.stage, case.state) == expected


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("fraudulent", ReasonCode.FRAUD),
        ("product_not_received", ReasonCode.NOT_RECEIVED),
        ("product_unacceptable", ReasonCode.NOT_AS_DESCRIBED),
        ("duplicate", None),
    ],
)
def test_reason_mapping(reason: str, code: ReasonCode | None) -> None:
    (case,) = ingest_dispute(dispute(reason=reason)).cases
    assert case.reason_code == code
    assert case.source_reason == reason


@pytest.mark.parametrize(("field", "value"), [("status", "escalated"), ("reason", "aliens")])
def test_unknown_values_raise(field: str, value: str) -> None:
    with pytest.raises(UnmappedValueError):
        ingest_dispute(dispute(**{field: value}))


def test_live_mode_refused_unless_allowed() -> None:
    with pytest.raises(LiveModeRefusedError):
        ingest_dispute(dispute(livemode=True))
    assert ingest_dispute(dispute(livemode=True), allow_live=True).cases[0].livemode


def test_evidence_presence_only_never_content() -> None:
    ev = dict(FIXTURES["dispute"]["evidence"])
    ev.update(shipping_tracking_number="1Z999", customer_name="Ada Example", receipt="file_1")
    (case,) = ingest_dispute(dispute(evidence=ev)).cases
    assert case.evidence_fields == ("customer_name", "receipt", "shipping_tracking_number")
    assert "Ada" not in case.model_dump_json()


def test_due_by_zero_means_no_response_allowed() -> None:
    details = dict(FIXTURES["dispute"]["evidence_details"], due_by=0, submission_count=2)
    (case,) = ingest_dispute(dispute(evidence_details=details)).cases
    assert case.evidence_due_at is None
    assert case.evidence_submitted


def test_expanded_charge_and_balance_transactions() -> None:
    reinstated = withdrawal(amount=1000, fee=0, bt_id="txn_r")
    d = dispute(
        charge=copy.deepcopy(FIXTURES["charge"]),
        status="won",
        balance_transactions=[withdrawal(), reinstated],
    )
    batch = ingest_dispute(d)
    assert batch.cases[0].payment_ref == FIXTURES["charge"]["id"]
    w, r = batch.movements
    assert (w.kind, w.amount_minor, w.fee_minor, w.net_minor) == (
        MovementKind.DISPUTE_DEBIT,
        -1000,
        1500,
        -2500,
    )
    assert r.kind is MovementKind.DISPUTE_CREDIT
    ledger = Ledger()
    for m in batch.movements:
        for kind, txn, ccy, postings in m.journal_postings("merchant:acct_test"):
            ledger.post(kind, txn, w.case_external_id, ccy, postings)
    assert ledger.balance("merchant:acct_test", "USD") == -1500
    assert ledger.balance("processor:fees", "USD") == 1500


def test_net_must_equal_amount_minus_fee() -> None:
    bt = withdrawal()
    bt["net"] = 0
    with pytest.raises(SchemaMismatchError):
        ingest_dispute(dispute(balance_transactions=[bt]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "1000"),
        ("amount", True),
        ("amount", 10.0),
        ("livemode", "false"),
        ("created", None),
        ("object", "charge"),
        ("currency", "usdx"),
        ("currency", "zzz"),
        ("amount", -5),
        ("created", 10**15),
        ("charge", 7),
    ],
)
def test_strict_types(field: str, value: object) -> None:
    with pytest.raises(IngestError):
        ingest_dispute(dispute(**{field: value}))


@pytest.mark.parametrize("missing", ["id", "amount", "status", "evidence_details", "charge"])
def test_missing_required_field(missing: str) -> None:
    d = dispute()
    del d[missing]
    with pytest.raises(SchemaMismatchError):
        ingest_dispute(d)


def test_json_bytes_input_and_limits() -> None:
    raw = json.dumps(FIXTURES["dispute"]).encode()
    assert ingest_dispute(raw).cases
    with pytest.raises(MalformedInputError):
        ingest_dispute(raw[:-1])
    with pytest.raises(InputTooLargeError):
        ingest_dispute(b"[" * 10_000 + b"]" * 10_000)
    with pytest.raises(MalformedInputError):
        ingest_dispute(b'{"a": NaN}')
    with pytest.raises(MalformedInputError):
        ingest_dispute(b'{"id": "a", "id": "b"}')
    with pytest.raises(MalformedInputError):
        ingest_dispute(b"\xff\xfe")
    with pytest.raises(InputTooLargeError):
        load_json(b" " * 100, max_bytes=10)


# --- fuzzing: parse or raise a typed error, never anything else ----------------

json_leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=False),
    st.text(max_size=20),
)
json_value = st.recursive(
    json_leaf,
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=8), c, max_size=3),
    max_leaves=10,
)


@settings(max_examples=150, deadline=None)
@given(st.binary(max_size=300))
def test_fuzz_bytes(data: bytes) -> None:
    with contextlib.suppress(IngestError):
        ingest_dispute(data)


def _paths(obj: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    out = [prefix]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _paths(v, (*prefix, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _paths(v, (*prefix, i))
    return out


BASE = dispute(balance_transactions=[withdrawal()], charge=copy.deepcopy(FIXTURES["charge"]))
PATHS = [p for p in _paths(BASE) if p]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.sampled_from(PATHS), json_value)
def test_fuzz_mutated_real_fixture(path: tuple[Any, ...], value: Any) -> None:
    d = copy.deepcopy(BASE)
    cur = d
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value
    try:
        batch = ingest_dispute(d)
    except IngestError:
        return
    for m in batch.movements:
        assert m.net_minor == m.amount_minor - m.fee_minor


# --- HTTP client (mock transport) ------------------------------------------------


def page(ids: list[str], has_more: bool) -> dict[str, Any]:
    return {
        "object": "list",
        "url": "/v1/disputes",
        "has_more": has_more,
        "data": [dispute(id=i) for i in ids],
    }


class Recorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def client(rec: Recorder, **kw: Any) -> StripeClient:
    return StripeClient(KEY, transport=httpx.MockTransport(rec), **kw)


def test_client_paginates_with_starting_after() -> None:
    rec = Recorder(
        [
            httpx.Response(200, json=page(["dp_1", "dp_2"], True)),
            httpx.Response(200, json=page(["dp_3"], False)),
        ]
    )
    with client(rec) as c:
        ids = [d.id for d in c.iter_disputes(page_size=2, created_gte=1700000000)]
    assert ids == ["dp_1", "dp_2", "dp_3"]
    first, second = rec.requests
    assert first.url.path == "/v1/disputes"
    assert first.url.params["limit"] == "2"
    assert first.url.params["created[gte]"] == "1700000000"
    assert "starting_after" not in first.url.params
    assert second.url.params["starting_after"] == "dp_2"
    assert first.headers["authorization"] == f"Bearer {KEY}"
    assert first.url.host == "api.stripe.com"


def test_client_get_dispute_and_max_pages() -> None:
    rec = Recorder([httpx.Response(200, json=dispute(id="dp_9"))])
    assert client(rec).get_dispute("dp_9").id == "dp_9"
    rec = Recorder([httpx.Response(200, json=page(["dp_1"], True)) for _ in range(3)])
    with pytest.raises(IngestError, match="pages"):
        list(client(rec).iter_disputes(max_pages=3))


def test_client_api_error_is_typed() -> None:
    body = {"error": {"type": "invalid_request_error", "message": "No such dispute"}}
    rec = Recorder([httpx.Response(404, json=body), httpx.Response(502, content=b"<html>")])
    c = client(rec)
    with pytest.raises(StripeAPIError) as e:
        c.get_dispute("dp_missing")
    assert e.value.status == 404
    assert e.value.error_type == "invalid_request_error"
    with pytest.raises(StripeAPIError):
        c.get_dispute("dp_x")


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", json.dumps({"object": "list", "data": [{"id": 1}]}).encode()],
)
def test_client_rejects_bad_bodies(body: bytes) -> None:
    rec = Recorder([httpx.Response(200, content=body)])
    with pytest.raises(IngestError):
        client(rec).list_disputes()


def test_client_response_size_limit() -> None:
    rec = Recorder([httpx.Response(200, content=b" " * 2000)])
    with pytest.raises(IngestError, match="limit"):
        client(rec, max_response_bytes=1000).list_disputes()


def test_client_transport_failure_is_typed() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    with pytest.raises(IngestError, match="ConnectError"):
        StripeClient(KEY, transport=httpx.MockTransport(boom)).list_disputes()


@pytest.mark.parametrize(
    "bad", ["", "sk_test_", "pk_test_abcdefghij", "sk_test_abc def12", "whsec_x"]
)
def test_client_rejects_malformed_keys(bad: str) -> None:
    with pytest.raises(IngestError):
        StripeClient(bad)


def test_client_refuses_live_keys_and_hides_key() -> None:
    live = "sk_live_" + "1" * 24
    with pytest.raises(LiveModeRefusedError):
        StripeClient(live)
    c = StripeClient(live, allow_live=True, transport=httpx.MockTransport(Recorder([])))
    assert live not in repr(c)
    assert KEY not in repr(client(Recorder([])))


@pytest.mark.parametrize("bad", ["../charges", "dp_1?x=1", "dp 1", ""])
def test_client_rejects_path_injection(bad: str) -> None:
    rec = Recorder([])
    with pytest.raises(IngestError):
        client(rec).get_dispute(bad)
    with pytest.raises(IngestError):
        client(rec).list_disputes(starting_after=bad or "a/b")
    assert rec.requests == []


@pytest.mark.parametrize("limit", [0, 101])
def test_client_limit_bounds(limit: int) -> None:
    with pytest.raises(IngestError):
        client(Recorder([])).list_disputes(limit=limit)
