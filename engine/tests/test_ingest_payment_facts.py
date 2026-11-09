"""Rule-relevant payment facts from exports: Stripe charge details (3-D Secure, AVS/CVC,
card, shipping, refunds), Adyen notification fields and CSV columns. Real inputs are
the vendored Stripe and Adyen fixtures (edited copies in memory); unknown means None."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from scruple.backtest.history import from_records, load_store
from scruple.backtest.view import (
    DECISION_TIME_FACTS,
    DEFAULTED_RULE_FIELDS,
    OUTCOME_TIME_FACTS,
    RULE_FIELD_FACTS,
    LookAheadError,
    PaymentIndex,
    build_view,
)
from scruple.domain import ReasonCode
from scruple.ingest.adyen import NotificationItem, ingest_notification, item_facts
from scruple.ingest.errors import IngestError
from scruple.ingest.models import CaseState, PaymentFacts
from scruple.ingest.pipeline import IngestOptions, ingest
from scruple.ingest.store import CaseStore
from scruple.ingest.stripe.mapping import THREE_DS_AUTHENTICATED, ingest_dispute
from scruple.ingest.stripe.wire import SCHEMA_OF, RefundList
from scruple.ingest.tabular import ColumnMapping, ImportConfig, StatusTarget, import_csv
from scruple.sim.models import Stage
from tests.fixtures.openapi_contract import check_model, load_schemas
from tests.test_ingest_adyen import CHARGEBACK, TEST_KEY, example, signed

HERE = Path(__file__).parent / "fixtures" / "stripe"
SCHEMAS = load_schemas(HERE / "spec3.subset.json")
FIX: dict[str, Any] = json.loads((HERE / "fixtures3.subset.json").read_text())["resources"]
OPENED = FIX["dispute"]["created"]


def charge(**over: Any) -> dict[str, Any]:
    c: dict[str, Any] = copy.deepcopy(FIX["charge"])
    c.update(over)
    return c


def dispute(ch: dict[str, Any] | str | None = None, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = copy.deepcopy(FIX["dispute"])
    d["charge"] = charge() if ch is None else ch
    d.update(over)
    return d


def facts(d: dict[str, Any]) -> PaymentFacts:
    f = ingest_dispute(d).cases[0].facts
    assert f is not None
    return f


def card(ch: dict[str, Any]) -> dict[str, Any]:
    c: dict[str, Any] = ch["payment_method_details"]["card"]
    return c


# --- contract ------------------------------------------------------------------------


def test_new_wire_models_are_in_the_vendored_spec() -> None:
    for name in (
        "payment_method_details",
        "payment_method_details_card",
        "payment_method_details_card_checks",
        "three_d_secure_details_charge",
        "shipping",
        "address",
        "refund",
    ):
        assert name in SCHEMAS and name in SCHEMA_OF.values()


def test_inline_schema_contract_catches_drift() -> None:
    class Wrong(BaseModel):
        data: tuple[int, ...]
        has_more: str

    assert len(check_model(Wrong, "charge#refunds", SCHEMAS, SCHEMA_OF)) == 2
    assert check_model(RefundList, "charge#refunds", SCHEMAS, SCHEMA_OF) == []


def test_three_ds_mapping_covers_the_spec_enum_exactly() -> None:
    enum = SCHEMAS["three_d_secure_details_charge"]["properties"]["result"]["enum"]
    assert set(THREE_DS_AUTHENTICATED) == set(enum)
    assert [k for k, v in THREE_DS_AUTHENTICATED.items() if v] == ["authenticated"]


def test_real_refund_fixture_parses() -> None:
    RefundList.model_validate({"data": [FIX["refund"]], "has_more": False})


# --- Stripe ----------------------------------------------------------------------------


def test_real_expanded_charge_facts() -> None:
    f = facts(dispute())
    assert f.card_brand == "visa" and f.card_funding == "credit" and f.card_country == "US"
    assert f.cvc_check == "pass" and f.avs_line1_check is None
    assert f.avs_postal_code_check is None
    # three_d_secure is an object with every field null: involved, outcome unknown.
    assert f.three_ds_result is None and f.three_ds_authenticated is None
    assert f.shipping_address_present is False and f.shipping_tracking_present is False
    assert f.refunded_before_dispute is False and f.refund_state == "none"
    assert f.amount_refunded_minor == 0
    assert f.mcc is None and f.delivery_confirmed is None
    assert f.enhanced_eligibility_types == ("visa_compelling_evidence_3",)
    assert f.is_charge_refundable is True and f.evidence_submission_count == 0


def test_dispute_without_expanded_charge_knows_only_dispute_fields() -> None:
    f = facts(dispute(FIX["dispute"]["charge"]))
    assert f.card_brand == "visa" and f.evidence_has_evidence is False
    for name in (
        "card_funding",
        "three_ds_authenticated",
        "cvc_check",
        "shipping_address_present",
        "refunded_before_dispute",
        "refund_state",
        "mcc",
    ):
        assert getattr(f, name) is None, name


@pytest.mark.parametrize(("result", "expected"), sorted(THREE_DS_AUTHENTICATED.items()))
def test_three_ds_results(result: str, expected: bool) -> None:
    ch = charge()
    card(ch)["three_d_secure"] = {"result": result, "version": "2.2.0"}
    f = facts(dispute(ch))
    assert f.three_ds_result == result and f.three_ds_authenticated is expected
    assert f.three_ds_version == "2.2.0"


def test_three_ds_null_means_not_used_and_unknown_value_means_unknown() -> None:
    ch = charge()
    card(ch)["three_d_secure"] = None
    assert facts(dispute(ch)).three_ds_authenticated is False
    card(ch)["three_d_secure"] = {"result": "a_new_value_stripe_adds"}
    f = facts(dispute(ch))
    assert f.three_ds_result == "a_new_value_stripe_adds" and f.three_ds_authenticated is None


def test_avs_and_cvc_checks_verbatim() -> None:
    ch = charge()
    card(ch)["checks"] = {
        "address_line1_check": "fail",
        "address_postal_code_check": "pass",
        "cvc_check": "unchecked",
    }
    f = facts(dispute(ch))
    assert (f.avs_line1_check, f.avs_postal_code_check, f.cvc_check) == (
        "fail",
        "pass",
        "unchecked",
    )
    card(ch)["checks"] = None
    assert facts(dispute(ch)).cvc_check is None


def test_shipping_presence_only() -> None:
    ch = charge(
        shipping={
            "address": {"line1": "1 Test Street", "postal_code": "94107", "country": "US"},
            "carrier": "UPS",
            "name": "Jenny Rosen",
            "phone": None,
            "tracking_number": "1Z999",
        }
    )
    f = facts(dispute(ch))
    assert f.shipping_address_present and f.shipping_tracking_present
    assert f.shipping_carrier == "UPS"
    assert "Test Street" not in f.model_dump_json() and "1Z999" not in f.model_dump_json()
    assert facts(dispute(charge(shipping=None))).shipping_address_present is False


def _refund(created: int, amount: int = 50, status: str = "succeeded") -> dict[str, Any]:
    r: dict[str, Any] = copy.deepcopy(FIX["refund"])
    r.update(created=created, amount=amount, status=status)
    return r


@pytest.mark.parametrize(
    ("refunds", "has_more", "refunded", "before", "state"),
    [
        ([], False, 0, False, "none"),
        ([_refund(OPENED - 10)], False, 50, True, "partial"),
        ([_refund(OPENED + 10)], False, 50, False, "partial"),
        ([_refund(OPENED - 10, status="failed")], False, 50, False, "partial"),
        ([_refund(OPENED - 10, amount=100)], False, 100, True, "full"),
        ([], True, 50, None, "partial"),  # incomplete list: when is unknown
    ],
)
def test_refund_before_dispute(
    refunds: list[dict[str, Any]], has_more: bool, refunded: int, before: bool | None, state: str
) -> None:
    ch = charge(amount_refunded=refunded)
    ch["refunds"] = {"object": "list", "data": refunds, "has_more": has_more, "url": "/x"}
    f = facts(dispute(ch))
    assert f.refunded_before_dispute is before and f.refund_state == state


def test_refunds_not_expanded_but_some_refunded_is_unknown() -> None:
    ch = charge(amount_refunded=10)
    ch.pop("refunds")
    assert facts(dispute(ch)).refunded_before_dispute is None


_values = st.one_of(st.none(), st.text(max_size=6), st.integers(-5, 5), st.booleans())


@given(
    path=st.sampled_from(
        [
            ("payment_method_details", "card", "checks", "cvc_check"),
            ("payment_method_details", "card", "three_d_secure", "result"),
            ("payment_method_details", "card", "three_d_secure"),
            ("payment_method_details", "card", "funding"),
            ("payment_method_details", "card"),
            ("payment_method_details",),
            ("shipping",),
            ("refunds",),
            ("amount_refunded",),
        ]
    ),
    value=_values,
    drop=st.booleans(),
)
def test_property_mutated_real_charge_maps_or_raises_typed(
    path: tuple[str, ...], value: Any, drop: bool
) -> None:
    ch = charge()
    node: Any = ch
    for p in path[:-1]:
        node = node[p]
    if drop:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
    try:
        f = facts(dispute(ch))
    except IngestError:
        return
    if drop and path[-1] in ("payment_method_details", "card"):
        assert f.card_funding is None and f.three_ds_authenticated is None and f.cvc_check is None


# --- Adyen -----------------------------------------------------------------------------


def test_adyen_facts_from_spec_example() -> None:
    res = ingest_notification(signed(example(CHARGEBACK)), [TEST_KEY])
    f = res.cases[0].facts
    assert f is not None
    assert (f.card_brand, f.payment_method_variant, f.defendable) == ("mc", "mc", True)
    assert f.auto_defended is None
    for name in ("three_ds_authenticated", "cvc_check", "mcc", "shipping_address_present"):
        assert getattr(f, name) is None


@pytest.mark.parametrize(("raw", "flag"), [("true", True), ("false", False), ("maybe", None)])
def test_adyen_flags(raw: str, flag: bool | None) -> None:
    item = example(CHARGEBACK)["notificationItems"][0]["NotificationRequestItem"]
    item["additionalData"]["defendable"] = raw
    assert item_facts(NotificationItem.model_validate(item)).defendable is flag


# --- CSV -------------------------------------------------------------------------------


def _cfg() -> ImportConfig:
    return ImportConfig(
        columns=ColumnMapping(
            external_id="id",
            payment_ref="pay",
            amount="amt",
            currency="ccy",
            status="st",
            reason="why",
            opened_at="at",
            mcc="mcc",
            three_ds="tds",
            delivery_confirmed="delivered",
            prior_refund="refunded",
        ),
        status_map={"lost": StatusTarget(stage=Stage.RESOLVED, state=CaseState.LOST)},
        reason_map={"inr": ReasonCode.NOT_RECEIVED},
    )


HEAD = "id,pay,amt,ccy,st,why,at,mcc,tds,delivered,refunded\n"


def test_csv_fact_columns() -> None:
    rep = import_csv(
        HEAD + "a,p1,1.00,USD,lost,inr,2026-01-01,5732,yes,true,0\n"
        "b,p2,1.00,USD,lost,inr,2026-01-01,,,,\n"
        "c,p3,1.00,USD,lost,inr,2026-01-01,5732,perhaps,true,0\n",
        _cfg(),
    )
    a, b = rep.cases
    assert a.facts == PaymentFacts(
        mcc="5732",
        three_ds_authenticated=True,
        delivery_confirmed=True,
        refunded_before_dispute=False,
    )
    assert b.facts == PaymentFacts()
    assert [(e.row, e.column) for e in rep.errors] == [(3, "tds")]


# --- the decision-time view and the backtest ---------------------------------------------


def test_view_shows_decision_time_facts_and_withholds_outcome_time_ones() -> None:
    case = ingest_dispute(dispute()).cases[0]
    hist = from_records([case], label="real fixture")
    v = build_view(hist.cases[0], PaymentIndex(hist.cases))
    assert v.card_funding == "credit" and v.cvc_check == "pass"
    assert v.three_ds_authenticated is None  # known to be unknown, not withheld
    for name in OUTCOME_TIME_FACTS:
        with pytest.raises(LookAheadError):
            v[name]
    assert set(DECISION_TIME_FACTS) <= set(v.known())
    assert set(DECISION_TIME_FACTS) | set(OUTCOME_TIME_FACTS) == set(PaymentFacts.model_fields)


def test_rule_view_reads_facts_and_reports_defaults() -> None:
    ch = charge()
    card(ch)["three_d_secure"] = {"result": "authenticated"}
    case = ingest_dispute(dispute(ch)).cases[0]
    hist = from_records([case], label="real fixture")
    v = build_view(hist.cases[0], PaymentIndex(hist.cases))
    rv = v.rule_view()
    assert rv["three_ds"] is True and rv["has_prior_refund"] is False
    assert set(v.defaulted_rule_fields()) == {"mcc", "delivery_confirmed", "currency_mismatch"}
    assert set(RULE_FIELD_FACTS) < set(DEFAULTED_RULE_FIELDS)


def test_csv_delivery_column_reaches_the_backtest(tmp_path: Path) -> None:
    from scruple.backtest import rule_candidate, run_backtest

    rows = "".join(
        f"c{i},p{i},20.00,USD,lost,inr,2026-01-{1 + i % 28:02d},5732,no,{i % 2 == 1!s},0\n"
        for i in range(40)
    )
    store = CaseStore(tmp_path / "s.db")
    ingest(HEAD + rows, IngestOptions(source="csv", import_config=_cfg()), store)
    store.close()
    hist = load_store(str(tmp_path / "s.db"))
    good = json.loads((Path("docs/pilot/demo/good.json")).read_text())
    rep = run_backtest(hist, rule_candidate(good))
    # The rule contests delivered INR claims: it fires on the 20 delivered cases only.
    assert rep.n_estimated == 20
    assert "delivery_confirmed" not in rep.rule_fields_defaulted
    assert rep.rule_fields_defaulted == {"currency_mismatch": 40}
