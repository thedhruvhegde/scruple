from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.sim.card_ledger import CardLedger, DoubleRefundError
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    Accept,
    ActionError,
    AgentAction,
    CardDispute,
    CaseMix,
    DisputeEngine,
    Escalate,
    PendingRefund,
    Refund,
    Represent,
    RequestInfo,
    drain,
    generate_cases,
)
from scruple.sim.evidence import EvidenceKind
from scruple.sim.models import Resolution, Stage

RACE = CaseMix(p_refund_race=1.0)


def base(stage: Stage = Stage.CHARGEBACK) -> CardDispute:
    return next(
        c for c in generate_cases(3, 200) if c.truth.claim_valid and c.opening_stage is stage
    )


def race(settle_offset: int, stage: Stage = Stage.CHARGEBACK) -> CardDispute:
    c = base(stage)
    rf = PendingRefund(
        amount_minor=c.amount_minor, settles_on=c.opened_on + timedelta(days=settle_offset)
    )
    return CardDispute.model_validate({**c.model_dump(), "refund_in_flight": rf.model_dump()})


def run(c: CardDispute) -> tuple[DisputeEngine, ManualClock]:
    clock = ManualClock()
    clock.set_date(c.opened_on)
    return DisputeEngine([c], clock), clock


# ------------------------------------------------------------------ ledger
def ledger() -> CardLedger:
    lg = CardLedger()
    lg.capture("t", "m", 1_000, "USD")
    return lg


def test_refund_is_strict() -> None:
    lg = ledger()
    lg.refund("t", "c", 600)
    with pytest.raises(DoubleRefundError, match="exceeds"):
        lg.refund("t", "c", 401)
    lg.refund("t", "c", 400)
    assert lg.headroom("t") == 0


def test_chargeback_debit_posts_but_is_flagged_when_it_double_refunds() -> None:
    lg = ledger()
    lg.refund("t", "c", 1_000)
    entry = lg.chargeback_debit("t", "c", 1_000)
    assert lg.violations[0].entry_id == entry.entry_id
    assert lg.violations[0].over_by == 1_000
    assert lg.over_credited() == {"t": 1_000}
    lg.to_merchant("chargeback_reversal", "t", "c", 1_000)
    assert lg.over_credited() == {}
    assert all(v == 0 for v in lg.currency_totals().values())


def test_partial_overlap_flags_only_the_excess() -> None:
    lg = ledger()
    lg.refund("t", "c", 300)
    lg.chargeback_debit("t", "c", 800)
    assert lg.violations[0].over_by == 100


def test_reversal_cannot_exceed_what_the_cardholder_holds() -> None:
    lg = ledger()
    with pytest.raises(ValueError, match="exceeds"):
        lg.to_merchant("chargeback_reversal", "t", "c", 1)


# ------------------------------------------------------------------ engine
def test_refund_settled_before_the_chargeback_is_caught_and_recoverable() -> None:
    c = race(-2)
    e, clock = run(c)
    assert [i.kind for i in e.incidents] == ["double_refund"]
    assert c.txn_id in e.ledger.over_credited()
    obs = e.observe(c.case_id)
    refund_docs = [d for d in obs.evidence if d.kind is EvidenceKind.REFUND_RECORD]
    assert refund_docs and refund_docs[0].fields["refund_settled"] == "Y"
    assert e.act(c.case_id, Represent(evidence_ids=(refund_docs[0].doc_id,))).ok
    drain(e, clock)
    out = e.outcome(c.case_id)
    assert out is not None and out.resolution is Resolution.WON
    assert out.correct and out.cardholder_net_minor == c.amount_minor
    assert e.ledger.over_credited() == {}


def test_accepting_a_double_refund_leaves_the_cardholder_paid_twice() -> None:
    c = race(-2)
    e, _ = run(c)
    assert e.act(c.case_id, Accept()).ok
    out = e.outcome(c.case_id)
    assert out is not None and not out.correct
    assert out.cardholder_net_minor == 2 * c.amount_minor


def test_refund_settling_after_the_chargeback_is_blocked() -> None:
    c = race(4)
    e, clock = run(c)
    assert not e.incidents
    clock.advance(days=4)
    e.sync()
    assert [i.kind for i in e.incidents] == ["refund_blocked"]
    assert e.ledger.returned(c.txn_id) == c.amount_minor
    assert [x.kind for x in e.ledger.entries].count("refund") == 0


def test_refund_settling_during_an_inquiry_closes_it() -> None:
    c = race(3, Stage.INQUIRY)
    e, clock = run(c)
    clock.advance(days=3)
    e.sync()
    out = e.outcome(c.case_id)
    assert out is not None and out.resolution is Resolution.ACCEPTED
    assert out.path == (Stage.INQUIRY, Stage.RESOLVED)
    assert out.correct and not e.incidents


def test_resolved_case_still_blocks_a_late_refund() -> None:
    c = race(5)
    e, clock = run(c)
    e.act(c.case_id, Accept())
    clock.advance(days=10)
    e.sync()
    assert [i.kind for i in e.incidents] == ["refund_blocked"]
    out = e.outcome(c.case_id)
    assert out is not None and out.correct


def test_agent_refund_during_a_chargeback_is_refused() -> None:
    c = base()
    e, _ = run(c)
    before = len(e.ledger.entries)
    r = e.act(c.case_id, Refund(amount_minor=c.amount_minor))
    assert r.error is ActionError.DOUBLE_REFUND
    assert len(e.ledger.entries) == before


def test_agent_refund_at_inquiry_resolves_it() -> None:
    c = base(Stage.INQUIRY)
    e, _ = run(c)
    r = e.act(c.case_id, Refund(amount_minor=c.amount_minor))
    assert r.ok and r.stage_after is Stage.RESOLVED
    assert r.info["refund_doc_id"] in {d.doc_id for d in e.observe(c.case_id).evidence}
    out = e.outcome(c.case_id)
    assert out is not None and out.resolution is Resolution.ACCEPTED and out.correct


def test_partial_refund_at_inquiry_keeps_it_open() -> None:
    c = base(Stage.INQUIRY)
    e, _ = run(c)
    assert e.act(c.case_id, Refund(amount_minor=1)).ok
    assert e.observe(c.case_id).stage is Stage.INQUIRY
    assert e.observe(c.case_id).refunded_minor == 1


def test_generator_races_only_valid_claims() -> None:
    cases = generate_cases(4, 300, RACE)
    for c in cases:
        if c.truth.claim_valid:
            assert c.refund_in_flight is not None
            assert c.refund_in_flight.settles_on >= c.processed_on
            assert abs((c.refund_in_flight.settles_on - c.opened_on).days) <= 10
        else:
            assert c.refund_in_flight is None
    assert all(c.refund_in_flight is None for c in generate_cases(4, 300))


def test_bad_refund_in_flight_rejected() -> None:
    c = base()
    with pytest.raises(ValueError, match="refund in flight"):
        CardDispute.model_validate(
            {
                **c.model_dump(),
                "refund_in_flight": {
                    "amount_minor": c.captured_minor + 1,
                    "settles_on": c.opened_on,
                },
            }
        )


ACTIONS: list[AgentAction] = [
    Accept(),
    Represent(),
    Escalate(),
    RequestInfo(),
    Refund(amount_minor=200),
    Refund(amount_minor=10**7),
]


@settings(max_examples=60, deadline=None)
@given(
    seed=st.integers(0, 10_000),
    plan=st.lists(
        st.tuples(st.integers(0, 5), st.sampled_from(ACTIONS), st.integers(0, 12)), max_size=30
    ),
)
def test_every_double_refund_is_caught(seed: int, plan: list[tuple[int, AgentAction, int]]) -> None:
    cases = sorted(generate_cases(seed, 6, RACE), key=lambda c: c.opened_on)
    clock = ManualClock()
    clock.set_date(cases[0].opened_on)
    e = DisputeEngine(cases, clock)
    for idx, action, wait in plan:
        cid = cases[idx].case_id
        # submit the case's own evidence when representing, like a careful agent
        if isinstance(action, Represent):
            action = Represent(evidence_ids=tuple(d.doc_id for d in e.observe(cid).evidence))
        e.act(cid, action)
        clock.advance(days=wait)
        e.sync()
    drain(e, clock)
    clock.advance(days=30)
    e.sync()
    assert all(v == 0 for v in e.ledger.currency_totals().values())
    flagged = {v.txn_id for v in e.ledger.violations}
    assert set(e.ledger.over_credited()) <= flagged
    for c in cases:
        assert e.ledger.returned(c.txn_id) >= 0
        refunds = sum(
            p.amount_minor
            for x in e.ledger.entries
            if x.kind == "refund" and x.txn_id == c.txn_id
            for p in x.postings
            if p.account.startswith("cardholder:")
        )
        assert refunds <= c.captured_minor  # merchant refunds alone never exceed capture
    by_case = {i.case_id for i in e.incidents if i.kind == "double_refund"}
    assert by_case == {v.case_id for v in e.ledger.violations}
