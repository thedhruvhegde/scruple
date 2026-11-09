"""Backtest (154): the decision-time view and the no-look-ahead guarantee."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.backtest import (
    BacktestConfig,
    DecisionView,
    History,
    LookAheadError,
    PaymentIndex,
    build_view,
    from_records,
    run_backtest,
)
from scruple.backtest.candidate import ModuleCandidate, rule_candidate
from scruple.backtest.view import OUTCOME_FACTS, facts_of
from scruple.calibration import DEFAULT_PROFILE
from scruple.domain import FIELD_TYPES, Action
from scruple.ingest.models import CaseState, ExternalCase, LedgerMovement, MovementKind
from scruple.sim.models import Stage

T0 = datetime(2025, 5, 1, tzinfo=UTC)
STATES = {
    "won": (Stage.RESOLVED, CaseState.WON),
    "lost": (Stage.RESOLVED, CaseState.LOST),
    "open": (Stage.CHARGEBACK, CaseState.OPEN),
    "inquiry": (Stage.INQUIRY, CaseState.OPEN),
}


def case(
    i: int,
    status: str = "lost",
    *,
    reason: str = "fraudulent",
    submitted: bool = False,
    amount: int = 10_000,
    payment: str | None = None,
    hours: float = 0.0,
    due_days: float | None = 18,
) -> ExternalCase:
    stage, state = STATES[status]
    opened = T0 + timedelta(hours=hours if hours else i)
    return ExternalCase(
        source="stripe",
        external_id=f"dp_{i}",
        payment_ref=payment or f"ch_{i}",
        stage=stage,
        state=state,
        reason_code=None,
        source_reason=reason,
        amount_minor=amount,
        currency="USD",
        opened_at=opened,
        evidence_due_at=opened + timedelta(days=due_days) if due_days is not None else None,
        evidence_fields=("receipt",) if submitted else (),
        evidence_submitted=submitted,
        evidence_past_due=False,
    )


def moves(
    c: ExternalCase, fee: int = 1_500, credit_days: int | None = None
) -> list[LedgerMovement]:
    out = [
        LedgerMovement(
            source="stripe",
            external_id=f"txn_{c.external_id}_d",
            case_external_id=c.external_id,
            payment_ref=c.payment_ref,
            kind=MovementKind.DISPUTE_DEBIT,
            amount_minor=-c.amount_minor,
            fee_minor=fee,
            net_minor=-c.amount_minor - fee,
            currency="USD",
            occurred_at=c.opened_at,
            category="dispute",
        )
    ]
    if credit_days is not None:
        out.append(
            LedgerMovement(
                source="stripe",
                external_id=f"txn_{c.external_id}_c",
                case_external_id=c.external_id,
                payment_ref=c.payment_ref,
                kind=MovementKind.DISPUTE_CREDIT,
                amount_minor=c.amount_minor,
                fee_minor=0,
                net_minor=c.amount_minor,
                currency="USD",
                occurred_at=c.opened_at + timedelta(days=credit_days),
                category="dispute_reversal",
            )
        )
    return out


def history(cases: list[ExternalCase], movements: list[LedgerMovement] | None = None) -> History:
    return from_records(cases, movements or [], label="test history (hand-made)")


def won_case() -> History:
    c = case(1, "won", submitted=True)
    return history([c], moves(c, credit_days=40))


# --- the guard ------------------------------------------------------------------------


@pytest.mark.parametrize("name", OUTCOME_FACTS)
def test_every_outcome_fact_raises(name: str) -> None:
    h = won_case()
    v = build_view(h.cases[0], PaymentIndex(h.cases))
    with pytest.raises(LookAheadError, match=name):
        getattr(v, name)
    with pytest.raises(LookAheadError):
        v[name]
    with pytest.raises(LookAheadError):
        v.get(name)
    assert name not in v and name in v.withheld()


def test_withheld_values_are_not_stored_anywhere() -> None:
    h = won_case()
    v = build_view(h.cases[0], PaymentIndex(h.cases))
    values: dict[str, Any] = object.__getattribute__(v, "_values")
    withheld: dict[str, Any] = object.__getattribute__(v, "_withheld")
    assert not set(values) & set(OUTCOME_FACTS)
    assert all(w is None or isinstance(w, datetime) for w in withheld.values())
    assert "won" not in repr(v) and "WON" not in repr(values)


def test_view_is_read_only_and_unknown_names_are_plain_errors() -> None:
    h = won_case()
    v = build_view(h.cases[0], PaymentIndex(h.cases))
    with pytest.raises(AttributeError):
        v.amount_minor = 1
    with pytest.raises(AttributeError):
        _ = v.no_such_fact
    with pytest.raises(KeyError):
        v["no_such_fact"]
    assert v.get("no_such_fact", 7) == 7


def test_later_postings_are_out_of_the_view() -> None:
    h = won_case()
    hc = h.cases[0]
    v = build_view(hc, PaymentIndex(h.cases))
    # one posting for the debit, one for its fee (scruple.ingest.ledger.from_movement)
    assert [p.merchant_net_minor for p in v.ledger] == [-10_000, -1_500]
    assert [p.fee_minor for p in v.ledger] == [0, 1_500]
    # deciding 50 days later, the reinstatement credit (day 40) is visible
    late = build_view(hc, PaymentIndex(h.cases), hc.case.opened_at + timedelta(days=50))
    assert [p.merchant_net_minor for p in late.ledger] == [-10_000, -1_500, 10_000]
    assert late.response_allowed is False  # the due date (day 18) has passed


def test_cannot_decide_before_the_case_opens() -> None:
    h = won_case()
    hc = h.cases[0]
    with pytest.raises(ValueError):
        build_view(hc, PaymentIndex(h.cases), hc.case.opened_at - timedelta(seconds=1))


def test_prior_disputes_count_only_earlier_cases() -> None:
    a = case(1, payment="ch_same", hours=1)
    b = case(2, payment="ch_same", hours=5)
    c = case(3, payment="ch_same", hours=9)
    h = history([c, a, b])
    idx = PaymentIndex(h.cases)
    counts = [build_view(hc, idx).prior_disputes_on_payment for hc in h.cases]
    assert counts == [0, 1, 2]
    assert [build_view(hc, idx).rule_view()["prior_dispute_on_txn"] for hc in h.cases] == [
        False,
        True,
        True,
    ]


def test_rule_view_has_exactly_the_rule_fields() -> None:
    h = won_case()
    rv = build_view(h.cases[0], PaymentIndex(h.cases)).rule_view()
    assert set(rv) == set(FIELD_TYPES)
    for k, t in FIELD_TYPES.items():
        assert isinstance(rv[k], t)
    assert rv["reason_code"] == "unmapped" and rv["evidence_on_time"] is True


def test_no_due_date_means_no_response_allowed() -> None:
    h = history([case(1, due_days=None)])
    v = build_view(h.cases[0], PaymentIndex(h.cases))
    assert v.response_allowed is False and v.days_to_due is None


def test_rule_view_refuses_currencies_without_a_table() -> None:
    c = case(1).model_copy(update={"currency": "JPY"})
    h = history([c])
    with pytest.raises(ValueError, match="JPY"):
        build_view(h.cases[0], PaymentIndex(h.cases)).rule_view()


# --- a peeking policy fails the backtest ---------------------------------------------


@pytest.mark.parametrize(
    "peek",
    [
        lambda v: v.state,
        lambda v: v["evidence_submitted"],
        lambda v: v.get("evidence_fields"),
        lambda v: v.historical_action,
        lambda v: v.ledger_after_decision,
    ],
)
def test_a_policy_that_peeks_at_the_future_raises(peek: Any) -> None:
    h = history([case(i, "lost") for i in range(12)] + [case(50, "won", submitted=True)])

    def agent(view: DecisionView) -> Action:
        peek(view)
        return Action.ACCEPT

    with pytest.raises(LookAheadError):
        run_backtest(h, ModuleCandidate(agent, "peeker"), profile=DEFAULT_PROFILE)


def test_an_honest_policy_runs() -> None:
    h = history([case(i, "lost") for i in range(12)])

    def agent(view: DecisionView) -> Action:
        return (
            Action.CONTEST if view.amount_minor > 5_000 and view.response_allowed else Action.ACCEPT
        )

    r = run_backtest(h, ModuleCandidate(agent, "honest"), profile=DEFAULT_PROFILE)
    assert r.n_included == 12


# --- property: nothing in a view is later than its decision time ----------------------


timeline = st.lists(
    st.tuples(
        st.sampled_from(sorted(STATES)),
        st.booleans(),
        st.integers(0, 2_000),  # hours after T0
        st.integers(0, 3),  # payment group
        st.one_of(st.none(), st.integers(1, 120)),  # credit day
    ),
    min_size=1,
    max_size=25,
)


@given(timeline, st.integers(0, 24 * 200))
def test_views_hold_only_facts_known_by_the_decision_time(
    rows: list[tuple[str, bool, int, int, int | None]], delay_h: int
) -> None:
    cases, movements = [], []
    for i, (status, sub, hours, pay, credit) in enumerate(rows):
        c = case(i, status, submitted=sub, payment=f"ch_g{pay}", hours=hours + 0.5)
        cases.append(c)
        if status != "inquiry":
            movements += moves(c, credit_days=credit if status == "won" else None)
    h = history(cases, movements)
    idx = PaymentIndex(h.cases)
    for hc in h.cases:
        at = hc.case.opened_at + timedelta(hours=delay_h)
        v = build_view(hc, idx, at)
        for f in facts_of(hc, idx, at, "merchant"):
            if f.name in v:
                assert f.known_at is not None and f.known_at <= at
            else:
                with pytest.raises(LookAheadError):
                    v[f.name]
        assert all(p.occurred_at <= at for p in v.ledger)
        earlier = sum(
            1
            for o in h.cases
            if o.case.payment_ref == hc.case.payment_ref
            and o.case.opened_at < at
            and o.case.case_id != hc.case.case_id
        )
        assert v.prior_disputes_on_payment == earlier


@given(timeline)
def test_rule_candidates_never_see_outcomes(
    rows: list[tuple[str, bool, int, int, int | None]],
) -> None:
    """Two histories that differ only in outcomes get identical rule decisions."""
    a = [case(i, s, submitted=sub, hours=h + 0.5) for i, (s, sub, h, _, _) in enumerate(rows)]
    b = [c.model_copy(update={"state": CaseState.WON, "evidence_submitted": True}) for c in a]
    cand = rule_candidate(
        {
            "rules": [
                {
                    "description": "contest big",
                    "conditions": [{"field": "amount_usd_minor", "op": "gt", "value": 1}],
                    "action": "contest",
                    "priority": 1,
                }
            ],
            "default_action": "accept",
        }
    )
    ha, hb = history(a), history(b)
    da = [cand.decide(build_view(x, PaymentIndex(ha.cases))) for x in ha.cases]
    db = [cand.decide(build_view(x, PaymentIndex(hb.cases))) for x in hb.cases]
    assert da == db


def test_decision_delay_is_applied() -> None:
    c = case(1, "won", submitted=True)
    h = history([c] + [case(i, "lost") for i in range(2, 14)], moves(c, credit_days=2))
    seen: list[int] = []

    def agent(view: DecisionView) -> None:
        seen.append(len(view.ledger))

    run_backtest(
        h,
        ModuleCandidate(agent, "count"),
        profile=DEFAULT_PROFILE,
        config=BacktestConfig(decision_delay_hours=72),
    )
    assert 3 in seen  # the credit at day 2 is known three days after opening
