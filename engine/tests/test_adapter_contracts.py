"""Adapter contracts (roadmap 172): every documented value of every enumerated field,
end to end, plus everything else.

For each external adapter, the documented values come from the vendored schema or
code table (tests/fixtures/*, each with its licence and source in a README). Every
documented value must map through the adapter's public entry point to a definite
result; every undocumented value must be refused explicitly with an `IngestError`
subclass (or, where the adapter documents it, reported rather than dropped), and
never surface as a KeyError, ValueError or a silent default.

Extends the per-adapter suites (tests/test_ingest_*.py), which check the tables
equal the specs; this checks the tables are actually used. Synthetic records built
from the vendored public fixtures only.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.ingest.adyen import EVENT_MAP, STATUS_MAP, ingest_notification
from scruple.ingest.bank import Direction, parse_bai2
from scruple.ingest.bank.bai2_codes import CREDIT_CODES, DEBIT_CODES, MISC_CODES, STATUS_CODES
from scruple.ingest.currency import MINOR_UNITS, minor_units, normalise_currency
from scruple.ingest.errors import IngestError, MalformedInputError, UnmappedValueError
from scruple.ingest.models import CaseState
from scruple.ingest.nacha import CREDIT_CODES as ACH_CREDIT
from scruple.ingest.nacha import DEBIT_CODES as ACH_DEBIT
from scruple.ingest.nacha import parse_nacha
from scruple.ingest.nacha_codes import RETURN_REASONS
from scruple.ingest.stripe import ingest_dispute
from scruple.ingest.stripe.mapping import REASON_MAP
from scruple.ingest.stripe.mapping import STATUS_MAP as STRIPE_STATUS
from tests.test_ingest_adyen import CHARGEBACK, SCHEMAS, SPEC, TEST_KEY, example, signed
from tests.test_ingest_bank import SVB
from tests.test_ingest_nacha import build
from tests.test_ingest_stripe import SCHEMAS as STRIPE_SCHEMAS
from tests.test_ingest_stripe import dispute

codes = st.text(alphabet=st.characters(codec="ascii"), min_size=0, max_size=24)


# ----------------------------------------------------------------------- Stripe
def _documented_reasons() -> set[str]:
    desc = STRIPE_SCHEMAS["dispute"]["properties"]["reason"]["description"]
    return set(re.findall(r"`([a-z_]+)`", desc.split("Learn more")[0]))


@pytest.mark.parametrize("reason", sorted(_documented_reasons()))
def test_stripe_every_documented_reason_maps_end_to_end(reason: str) -> None:
    (case,) = ingest_dispute(dispute(reason=reason)).cases
    assert case.source_reason == reason
    assert case.reason_code == REASON_MAP[reason]


@pytest.mark.parametrize("status", STRIPE_SCHEMAS["dispute"]["properties"]["status"]["enum"])
def test_stripe_every_spec_status_maps_end_to_end(status: str) -> None:
    (case,) = ingest_dispute(dispute(status=status)).cases
    assert (case.stage, case.state) == STRIPE_STATUS[status]


@settings(max_examples=60)
@given(field=st.sampled_from(["status", "reason"]), value=codes)
def test_stripe_undocumented_values_are_refused(field: str, value: str) -> None:
    documented = set(STRIPE_STATUS) if field == "status" else set(REASON_MAP)
    if value in documented:
        return
    with pytest.raises(IngestError):
        ingest_dispute(dispute(**{field: value}))


# ------------------------------------------------------------------------ Adyen
@pytest.mark.parametrize("event", sorted(SPEC["x-scruple-dispute-webhooks"]))
def test_adyen_every_dispute_event_maps_end_to_end(event: str) -> None:
    env = example(CHARGEBACK)
    env["notificationItems"][0]["NotificationRequestItem"]["eventCode"] = event
    env["notificationItems"][0]["NotificationRequestItem"]["additionalData"].pop(
        "disputeStatus", None
    )
    res = ingest_notification(signed(env), [TEST_KEY])
    (case,) = res.cases
    assert (case.stage, case.state) == EVENT_MAP[event]
    assert case.source_reason == event and res.skipped == ()


@pytest.mark.parametrize(
    "status",
    SCHEMAS["DisputesNotificationAdditionalData"]["properties"]["disputeStatus"]["enum"],
)
def test_adyen_every_spec_dispute_status_maps(status: str) -> None:
    env = example(CHARGEBACK)
    env["notificationItems"][0]["NotificationRequestItem"]["additionalData"]["disputeStatus"] = (
        status
    )
    (case,) = ingest_notification(signed(env), [TEST_KEY]).cases
    assert case.state is STATUS_MAP[status]


@settings(max_examples=60)
@given(codes)
def test_adyen_other_event_codes_are_reported_not_dropped(code: str) -> None:
    if code in EVENT_MAP:
        return
    env = example(CHARGEBACK)
    env["notificationItems"][0]["NotificationRequestItem"]["eventCode"] = code
    try:
        res = ingest_notification(signed(env), [TEST_KEY])
    except IngestError:
        return  # refused explicitly (e.g. a code the signing string cannot carry)
    assert res.cases == ()
    assert [s.reason for s in res.skipped] == ["not a dispute event"]


@settings(max_examples=60)
@given(codes)
def test_adyen_undocumented_dispute_status_is_refused(status: str) -> None:
    if status in STATUS_MAP:
        return
    env = example(CHARGEBACK)
    env["notificationItems"][0]["NotificationRequestItem"]["additionalData"]["disputeStatus"] = (
        status
    )
    with pytest.raises(IngestError):
        ingest_notification(signed(env), [TEST_KEY])


# ------------------------------------------------------------------------ NACHA
@pytest.mark.parametrize("reason", sorted(RETURN_REASONS))
def test_nacha_every_return_reason_round_trips_with_its_title(reason: str) -> None:
    (b,) = parse_nacha(build([(26, "09100001", 1234, reason)])).batches
    (e,) = b.entries
    assert e.returned is not None
    assert (e.returned.reason_code, e.returned.reason_title) == (reason, RETURN_REASONS[reason])


@settings(max_examples=60)
@given(st.text(alphabet="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=2))
def test_nacha_other_return_codes_are_kept_but_flagged_unknown(suffix: str) -> None:
    """Custom return codes exist (moov-io's own fixture uses one), so an unknown code
    is kept, and flagged: it carries no title."""
    code = f"R{suffix}"
    (b,) = parse_nacha(build([(26, "09100001", 1, code)])).batches
    ret = b.entries[0].returned
    assert ret is not None and ret.reason_code == code
    assert (ret.reason_title is None) == (code not in RETURN_REASONS)


@pytest.mark.parametrize("code", sorted(ACH_CREDIT | ACH_DEBIT))
def test_nacha_every_transaction_code_has_a_direction(code: int) -> None:
    (b,) = parse_nacha(build([(code, "09100001", 500, None)])).batches
    assert b.entries[0].is_debit is (code in ACH_DEBIT)


def test_nacha_every_other_transaction_code_is_refused() -> None:
    others = sorted(set(range(100)) - ACH_CREDIT - ACH_DEBIT)
    assert len(others) == 70
    for code in others:  # one test, all 70 codes (not 70 tests)
        with pytest.raises(UnmappedValueError, match="unknown transaction code"):
            parse_nacha(build([(code, "09100001", 500, None)]))


# ------------------------------------------------------------------------- BAI2
def _svb_with_code(code: str) -> str:
    return SVB.replace("16,142,490196", f"16,{code},490196", 1)


@pytest.mark.parametrize("code", sorted(CREDIT_CODES | DEBIT_CODES | MISC_CODES))
def test_bai2_every_transaction_code_maps_to_its_direction(code: str) -> None:
    a, _ = parse_bai2(_svb_with_code(code))
    (e,) = a.entries
    expected = (
        Direction.CREDIT
        if code in CREDIT_CODES
        else Direction.DEBIT
        if code in DEBIT_CODES
        else None
    )
    assert e.type_code == code and e.direction is expected


@pytest.mark.parametrize("code", sorted(STATUS_CODES))
def test_bai2_status_codes_are_refused_as_transactions(code: str) -> None:
    with pytest.raises(MalformedInputError, match="status code"):
        parse_bai2(_svb_with_code(code))


ALL_BAI2 = CREDIT_CODES | DEBIT_CODES | MISC_CODES | STATUS_CODES


def test_bai2_every_code_outside_the_table_is_refused() -> None:
    others = sorted({f"{n:03d}" for n in range(1000)} - ALL_BAI2)
    assert len(others) > 300
    for code in others:  # one test, every unlisted three-digit code
        with pytest.raises(UnmappedValueError, match="unknown type code"):
            parse_bai2(_svb_with_code(code))


# --------------------------------------------------------------------- currency
@pytest.mark.parametrize("ccy", sorted(MINOR_UNITS))
def test_every_iso_currency_is_accepted_in_any_case(ccy: str) -> None:
    assert normalise_currency(ccy.lower()) == ccy
    assert minor_units(ccy) == MINOR_UNITS[ccy]
    (case,) = ingest_dispute(dispute(currency=ccy.lower())).cases
    assert case.currency == ccy


@settings(max_examples=100)
@given(st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=3, max_size=3))
def test_codes_outside_iso_4217_are_refused(code: str) -> None:
    if code in MINOR_UNITS:
        return
    with pytest.raises(UnmappedValueError):
        minor_units(code)
    with pytest.raises(IngestError):
        ingest_dispute(dispute(currency=code.lower()))


def test_contract_tables_are_used_not_just_declared() -> None:
    """Every table this file walks is non-trivial (guards against an empty import)."""
    sizes: dict[str, Any] = {
        "stripe status": STRIPE_STATUS,
        "stripe reason": REASON_MAP,
        "adyen events": EVENT_MAP,
        "adyen status": STATUS_MAP,
        "nacha returns": RETURN_REASONS,
        "bai2 codes": ALL_BAI2,
        "iso 4217": MINOR_UNITS,
    }
    assert all(len(v) >= 8 for v in sizes.values()), {k: len(v) for k, v in sizes.items()}
    assert CaseState.OPEN in {s for _, s in EVENT_MAP.values()}
    assert copy.deepcopy(SPEC)["x-scruple-dispute-webhooks"]
