"""Calibration aggregates (153): small-cell suppression and no row-level output."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.calibration import (
    OTHER,
    CalibrationConfig,
    HistoryAggregates,
    InsufficientHistoryError,
    Rate,
    compute_aggregates,
    suppress_counts,
    suppress_rate,
)
from scruple.calibration.aggregates import amount_bin
from scruple.calibration.facts import contested, is_inquiry_level, is_resolved_chargeback
from scruple.ingest.models import CaseState, ExternalCase, LedgerMovement, MovementKind
from scruple.sim.models import Stage

T0 = datetime(2025, 3, 1, tzinfo=UTC)
REASONS = ("fraudulent", "general", "product_not_received", "rare_a", "rare_b")
FIELDS = ("receipt", "shipping_tracking_number", "customer_purchase_ip")
OUTCOMES = (
    (Stage.INQUIRY, CaseState.OPEN),
    (Stage.RESOLVED, CaseState.CLOSED),
    (Stage.CHARGEBACK, CaseState.OPEN),
    (Stage.RESOLVED, CaseState.WON),
    (Stage.RESOLVED, CaseState.LOST),
    (Stage.RESOLVED, CaseState.ACCEPTED),
    (Stage.REPRESENTMENT, CaseState.LOST),
)


def make_case(
    i: int,
    reason: str = "fraudulent",
    outcome: tuple[Stage, CaseState] = (Stage.RESOLVED, CaseState.LOST),
    submitted: bool = False,
    fields: tuple[str, ...] = (),
    amount: int = 5_000,
    currency: str = "USD",
    late: bool = False,
    due: bool = True,
) -> ExternalCase:
    stage, state = outcome
    return ExternalCase(
        source="test",
        external_id=f"MARKER-ID-{i:05d}",
        payment_ref=f"MARKER-PAY-{i:05d}",
        merchant_ref=f"MARKER-MERCHANT-{i:05d}",
        stage=stage,
        state=state,
        reason_code=None,
        source_reason=reason,
        network="visa",
        amount_minor=amount,
        currency=currency,
        opened_at=T0 + timedelta(hours=i),
        evidence_due_at=T0 + timedelta(hours=i, days=18) if due else None,
        evidence_fields=fields,
        evidence_submitted=submitted,
        evidence_past_due=late,
    )


case_rows = st.tuples(
    st.sampled_from(REASONS),
    st.sampled_from(OUTCOMES),
    st.booleans(),
    st.sets(st.sampled_from(FIELDS)).map(lambda s: tuple(sorted(s))),
    st.integers(1, 3_000_000),
    st.sampled_from(("USD", "USD", "EUR")),
    st.booleans(),
    st.booleans(),
)


def build(rows: list[tuple[object, ...]]) -> list[ExternalCase]:
    out = []
    for i, (reason, outcome, sub, fields, amount, ccy, late, due) in enumerate(rows):
        out.append(
            make_case(
                i,
                reason=str(reason),
                outcome=outcome,  # type: ignore[arg-type]
                submitted=bool(sub),
                fields=fields if sub else (),  # type: ignore[arg-type]
                amount=int(amount),  # type: ignore[call-overload]
                currency=str(ccy),
                late=bool(late) and bool(sub),
                due=bool(due),
            )
        )
    return out


# --- suppression of one table -------------------------------------------------------


@given(
    st.dictionaries(st.text(min_size=1, max_size=6), st.integers(0, 60), max_size=12),
    st.integers(2, 25),
)
def test_suppression_never_emits_a_cell_below_k(counts: dict[str, int], k: int) -> None:
    t = suppress_counts(counts, k)
    total = sum(counts.values())
    assert all(v >= k for v in t.cells.values())
    assert t.suppressed == 0 or t.suppressed >= k
    if total >= k:
        assert t.total == total and not t.withheld
    else:
        assert t.total == 0 and t.cells == {} and t.withheld == (total > 0)
    assert set(t.cells) <= {key for key, v in counts.items() if v > 0}
    # every published cell is the true count of that key
    assert all(counts[key] == v for key, v in t.cells.items())
    # deterministic, and independent of the input's order
    assert suppress_counts(dict(reversed(list(counts.items()))), k) == t


def test_complementary_suppression_hides_a_lone_small_cell() -> None:
    t = suppress_counts({"a": 50, "b": 12, "c": 3}, 10)
    # 'c' alone would be total - a - b; so the smallest kept cell joins it
    assert t.cells == {"a": 50} and t.suppressed == 15


def test_suppression_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        suppress_counts({"a": 3}, 1)
    with pytest.raises(ValueError):
        suppress_counts({"a": -1}, 5)


@given(st.integers(0, 80), st.integers(0, 80), st.integers(2, 30))
def test_rate_needs_k_successes_and_k_failures(s: int, f: int, k: int) -> None:
    r = suppress_rate(s, s + f, k)
    if s >= k and f >= k:
        assert r is not None and r.successes == s and r.trials == s + f
        lo, hi = r.wilson()
        assert 0 <= lo <= r.value <= hi <= 1
    else:
        assert r is None


def test_rate_validation() -> None:
    with pytest.raises(ValueError):
        Rate(successes=5, trials=4)


def test_amount_bins_use_fixed_edges() -> None:
    edges = (100, 1_000)
    assert [amount_bin(a, edges) for a in (0, 99, 100, 999, 1_000, 10**9)] == [
        "0-100",
        "0-100",
        "100-1000",
        "100-1000",
        "1000+",
        "1000+",
    ]
    with pytest.raises(ValueError):
        CalibrationConfig(amount_edges=(5, 5))
    with pytest.raises(ValueError):
        CalibrationConfig(amount_edges=())


# --- whole histories ---------------------------------------------------------------


@given(st.lists(case_rows, min_size=0, max_size=300), st.sampled_from((2, 5, 10, 20)))
def test_no_emitted_count_is_below_k(rows: list[tuple[object, ...]], k: int) -> None:
    cases = build(rows)
    if len(cases) < k:
        with pytest.raises(InsufficientHistoryError):
            compute_aggregates(cases, config=CalibrationConfig(k=k))
        return
    aggs = compute_aggregates(cases, config=CalibrationConfig(k=k))
    for path, v in aggs.cells():
        assert v == 0 or v >= k, f"{path} = {v} is below k = {k}"
    assert aggs.n_cases == len(cases)


@given(st.lists(case_rows, min_size=10, max_size=200))
def test_aggregates_contain_no_row_level_values(rows: list[tuple[object, ...]]) -> None:
    cases = build(rows)
    text = compute_aggregates(cases).model_dump_json()
    assert "MARKER" not in text  # ids, payment and merchant refs never leave
    for c in cases:
        assert c.opened_at.isoformat() not in text


@given(st.lists(case_rows, min_size=10, max_size=150), st.randoms(use_true_random=False))
def test_aggregates_are_order_free_and_deterministic(
    rows: list[tuple[object, ...]], rnd: object
) -> None:
    cases = build(rows)
    shuffled = list(cases)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    a = compute_aggregates(cases)
    assert compute_aggregates(shuffled) == a
    assert compute_aggregates(cases).model_dump_json() == a.model_dump_json()


def test_round_trip_through_json() -> None:
    cases = build([("fraudulent", OUTCOMES[3], True, ("receipt",), 900, "USD", False, True)] * 30)
    a = compute_aggregates(cases)
    assert HistoryAggregates.model_validate_json(a.model_dump_json()) == a


def test_known_counts() -> None:
    cases = [make_case(i, "fraudulent", (Stage.RESOLVED, CaseState.WON), True) for i in range(15)]
    cases += [
        make_case(100 + i, "fraudulent", (Stage.RESOLVED, CaseState.LOST), True) for i in range(12)
    ]
    cases += [make_case(200 + i, "fraudulent") for i in range(20)]  # accepted (lost, no evidence)
    cases += [make_case(300 + i, "rare_a") for i in range(4)]
    cases += [make_case(400 + i, "rare_b") for i in range(7)]
    a = compute_aggregates(cases)
    assert a.reasons.cells == {"fraudulent": 47} and a.reasons.suppressed == 11
    assert a.win_by_reason["fraudulent"] == Rate(successes=15, trials=27)
    assert a.contest_by_reason["fraudulent"] == Rate(successes=27, trials=47)
    # the pooled small reasons are one OTHER group (11 >= k); 0 contested: no rate
    assert OTHER not in a.win_by_reason
    assert a.deadlines.cells == {"not_submitted": 31, "submitted_on_time": 27}


def test_rates_with_few_failures_are_suppressed() -> None:
    cases = [make_case(i, "general", (Stage.RESOLVED, CaseState.WON), True) for i in range(40)]
    cases += [
        make_case(50 + i, "general", (Stage.RESOLVED, CaseState.LOST), True) for i in range(3)
    ]
    a = compute_aggregates(cases)
    assert "general" not in a.win_by_reason and a.win_overall is None


def test_amounts_and_fees_use_the_main_currency() -> None:
    cases = [make_case(i, amount=1_000 * (i + 1)) for i in range(30)]
    cases += [make_case(100 + i, currency="EUR", amount=7) for i in range(12)]
    moves = [
        LedgerMovement(
            source="test",
            external_id=f"m{i}",
            case_external_id=f"MARKER-ID-{i:05d}",
            payment_ref=f"MARKER-PAY-{i:05d}",
            kind=MovementKind.DISPUTE_DEBIT,
            amount_minor=-1_000,
            fee_minor=1_500,
            net_minor=-2_500,
            currency="USD",
            occurred_at=T0,
            category="dispute",
        )
        for i in range(12)
    ]
    a = compute_aggregates(cases, moves)
    assert a.currency == "USD" and a.amount_n == 30
    assert a.debit_fee_mean == 1_500 and a.debit_fee_n == 12
    assert a.amount_bins.total == 30
    assert compute_aggregates(cases, moves[:5]).debit_fee_mean is None


def test_facts_reading() -> None:
    assert is_inquiry_level(make_case(1, outcome=(Stage.INQUIRY, CaseState.OPEN)))
    assert is_inquiry_level(make_case(1, outcome=(Stage.RESOLVED, CaseState.CLOSED)))
    lost = make_case(1, outcome=(Stage.RESOLVED, CaseState.LOST))
    assert is_resolved_chargeback(lost) and not contested(lost)
    assert contested(make_case(1, outcome=(Stage.REPRESENTMENT, CaseState.LOST)))
    assert contested(make_case(1, outcome=(Stage.RESOLVED, CaseState.WON)))
    assert not contested(make_case(1, outcome=(Stage.RESOLVED, CaseState.ACCEPTED)))
    assert contested(make_case(1, outcome=(Stage.RESOLVED, CaseState.ACCEPTED), submitted=True))


def test_json_has_only_aggregate_keys() -> None:
    cases = build([("general", OUTCOMES[4], True, ("receipt",), 50, "USD", False, True)] * 25)
    doc = json.loads(compute_aggregates(cases).model_dump_json())
    assert set(doc) == set(HistoryAggregates.model_fields)
