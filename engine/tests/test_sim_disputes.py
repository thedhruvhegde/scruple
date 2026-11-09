from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from scruple.hashing import canonical_json
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    ACTION_KINDS,
    Accept,
    ActionError,
    AgentAction,
    CardDispute,
    DisputeEngine,
    Escalate,
    Represent,
    Truth,
    TruthIssuer,
    drain,
    generate_cases,
)
from scruple.sim.models import Resolution, Stage
from scruple.sim.networks import Network


def _bytes(seed: int, n: int = 200) -> str:
    return canonical_json([c.model_dump(mode="json") for c in generate_cases(seed, n)])


def start_engine(seed: int, n: int) -> tuple[DisputeEngine, ManualClock, list[CardDispute]]:
    cases = generate_cases(seed, n)
    clock = ManualClock()
    clock.set_date(min(c.opened_on for c in cases))
    return DisputeEngine(cases, clock), clock, sorted(cases, key=lambda c: c.opened_on)


def test_generation_is_deterministic() -> None:
    assert _bytes(3) == _bytes(3)
    assert _bytes(3) != _bytes(4)


def test_generated_cases_are_unique_and_valid() -> None:
    cases = generate_cases(1, 500)
    assert len({c.case_id for c in cases}) == 500
    assert {c.network for c in cases} == set(Network)
    assert {c.opening_stage for c in cases} == {Stage.INQUIRY, Stage.CHARGEBACK}
    assert all(not c.spec.card_present for c in cases)


def test_view_hides_ground_truth() -> None:
    c = generate_cases(1, 1)[0]
    assert "truth" not in c.view()
    e, _, _ = start_engine(1, 1)
    obs = e.observe(c.case_id)
    assert "truth" not in obs.case and "claim_valid" not in canonical_json(
        obs.model_dump(mode="json")
    )


def test_case_validation() -> None:
    c = generate_cases(1, 1)[0]
    data = c.model_dump()
    with pytest.raises(ValidationError, match="more than was captured"):
        CardDispute.model_validate({**data, "amount_minor": c.captured_minor + 1})
    with pytest.raises(ValidationError, match="filing window"):
        CardDispute.model_validate({**data, "opened_on": c.processed_on + timedelta(days=400)})
    with pytest.raises(ValidationError, match="filing window"):
        CardDispute.model_validate({**data, "opened_on": c.processed_on - timedelta(days=1)})
    with pytest.raises(ValidationError, match="cannot open"):
        CardDispute.model_validate({**data, "opening_stage": Stage.ARBITRATION})
    with pytest.raises(ValidationError, match="no liability"):
        CardDispute.model_validate({**data, "truth": Truth(claim_valid=False, liability_minor=1)})
    with pytest.raises(KeyError):
        CardDispute.model_validate({**data, "reason_code": "99.9"})


def test_duplicate_case_ids_rejected() -> None:
    c = generate_cases(1, 1)[0]
    with pytest.raises(ValueError, match="duplicate"):
        DisputeEngine([c, c], ManualClock())


def test_actions_parse_from_json() -> None:
    ta: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)
    assert ta.validate_python({"kind": "represent", "evidence_ids": ["a"]}) == Represent(
        evidence_ids=("a",)
    )
    assert set(ACTION_KINDS) >= {"accept", "represent", "escalate"}
    with pytest.raises(ValidationError):
        ta.validate_python({"kind": "launder"})


def test_refused_actions_change_nothing() -> None:
    e, clock, cases = start_engine(2, 30)
    c = next(x for x in cases if x.opening_stage is Stage.CHARGEBACK)
    clock.set_date(c.opened_on)
    before = len(e.ledger.entries)
    r = e.act(c.case_id, Escalate())
    assert not r.ok and r.error is ActionError.NOT_ALLOWED
    assert len(e.ledger.entries) == before
    assert e.act("nope", Accept()).error is ActionError.UNKNOWN_CASE
    assert e.act(c.case_id, Accept()).ok
    assert e.act(c.case_id, Accept()).error is ActionError.RESOLVED


def test_cannot_act_while_issuer_is_deciding() -> None:
    e, clock, cases = start_engine(2, 30)
    c = next(x for x in cases if x.opening_stage is Stage.CHARGEBACK)
    clock.set_date(c.opened_on)
    assert e.act(c.case_id, Represent()).ok
    r = e.act(c.case_id, Accept())
    assert r.error is ActionError.AWAITING_OTHER_PARTY
    assert e.observe(c.case_id).allowed_actions == ()


def test_cannot_act_before_case_opens() -> None:
    cases = generate_cases(5, 5)
    clock = ManualClock()
    clock.set_date(min(c.processed_on for c in cases))
    e = DisputeEngine(cases, clock)
    assert e.act(cases[0].case_id, Accept()).error is ActionError.NOT_OPEN


def test_idle_merchant_loses_every_chargeback() -> None:
    e, clock, cases = start_engine(6, 60)
    drain(e, clock)
    for c in cases:
        out = e.outcome(c.case_id)
        assert out is not None and out.resolution is Resolution.LOST
        assert out.cardholder_net_minor == c.amount_minor


def test_deadline_miss_happens_the_day_after_the_due_date() -> None:
    e, clock, cases = start_engine(6, 60)
    c = next(x for x in cases if x.opening_stage is Stage.CHARGEBACK)
    clock.set_date(c.opened_on)
    due = e.observe(c.case_id).deadline
    assert due is not None
    clock.set_date(due.due)
    assert not e.is_resolved(c.case_id)
    assert e.act(c.case_id, Accept()).ok  # acting on the due date is on time


def test_truth_issuer_makes_representing_invalid_claims_correct() -> None:
    cases = sorted(generate_cases(8, 120), key=lambda c: c.opened_on)
    clock = ManualClock()
    clock.set_date(cases[0].opened_on)
    e = DisputeEngine(cases, clock, issuer=TruthIssuer())
    for c in cases:
        clock.set_date(c.opened_on)
        e.act(c.case_id, Represent() if not c.truth.claim_valid else Accept())
    # inquiries on valid claims need a second action once they become chargebacks
    drain_with(e, clock)
    for c in cases:
        out = e.outcome(c.case_id)
        assert out is not None and out.correct, (c.case_id, out)


def drain_with(e: DisputeEngine, clock: ManualClock) -> None:
    for _ in range(400):
        for cid in e.open_cases():
            if "accept" in e.observe(cid).allowed_actions:
                e.act(cid, Accept())
        if not e.open_cases():
            return
        clock.advance(days=1)


# ---------------------------------------------------------------- properties
BY_KIND: dict[str, AgentAction] = {
    "accept": Accept(),
    "represent": Represent(),
    "escalate": Escalate(),
}
action_st = st.sampled_from([Accept(), Represent(), Represent(evidence_ids=("x",)), Escalate()])


@settings(max_examples=60, deadline=None)
@given(
    seed=st.integers(0, 10_000),
    plan=st.lists(
        st.tuples(st.integers(0, 7), action_st, st.integers(0, 20)), min_size=1, max_size=40
    ),
)
def test_ledger_balances_after_any_action_sequence(
    seed: int, plan: list[tuple[int, AgentAction, int]]
) -> None:
    e, clock, cases = start_engine(seed, 8)
    for idx, action, wait in plan:
        e.act(cases[idx].case_id, action)
        clock.advance(days=wait)
        e.sync()
        check_ledger(e, cases)
    drain(e, clock)
    check_ledger(e, cases)
    for c in cases:
        out = e.outcome(c.case_id)
        assert out is not None
        assert out.path[-1] is Stage.RESOLVED
        assert out.cardholder_net_minor in (0, c.amount_minor)


def check_ledger(e: DisputeEngine, cases: list[CardDispute]) -> None:
    assert all(v == 0 for v in e.ledger.currency_totals().values())
    for entry in e.ledger.entries:
        assert sum(p.amount_minor for p in entry.postings) == 0
    for c in cases:
        assert 0 <= e.ledger.returned(c.txn_id) <= e.ledger.captured(c.txn_id)
        assert e.ledger.balance(f"cardholder:{c.txn_id}", c.currency) == (
            e.ledger.returned(c.txn_id) - c.captured_minor
        )


@settings(max_examples=40, deadline=None)
@given(seed=st.integers(0, 10_000), waits=st.lists(st.integers(0, 15), max_size=30))
def test_history_dates_never_go_backwards(seed: int, waits: list[int]) -> None:
    e, clock, cases = start_engine(seed, 6)
    for i, w in enumerate(waits):
        cid = cases[i % len(cases)].case_id
        acts = e.observe(cid).allowed_actions
        if acts:
            e.act(cid, BY_KIND[acts[-1]])
        clock.advance(days=w)
    drain(e, clock)
    for c in cases:
        hist = e.observe(c.case_id).history
        days = [t.on for t in hist]
        assert days == sorted(days)
        assert all(d >= c.opened_on for d in days)


def test_open_dates_are_sane() -> None:
    for c in generate_cases(11, 300):
        assert c.processed_on <= c.opened_on
        assert c.opened_on - c.processed_on <= timedelta(days=120)
        assert c.processed_on >= date(2026, 1, 5)
