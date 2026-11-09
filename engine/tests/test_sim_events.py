"""Event-sourced dispute engine (036): state = fold(events), replay is exact."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.sim.deadlines import ManualClock, today
from scruple.sim.disputes import (
    DEFAULT_MIX,
    FULL_MIX,
    Accept,
    ActionApplied,
    AgentAction,
    CardDispute,
    CaseCaptured,
    DisputeEngine,
    EngineEvent,
    Escalate,
    Refund,
    ReplayError,
    Represent,
    RequestInfo,
    StageFired,
    drain,
    generate_cases,
)
from scruple.sim.evidence import EvidenceDocument
from scruple.sim.lifecycle import Event
from scruple.sim.merchants import generate_merchants
from scruple.sim.models import Stage

# (case index, action code, fraction for refunds / evidence, days to advance first)
Plan = list[tuple[int, int, float, int]]
plans = st.lists(
    st.tuples(
        st.integers(0, 7),
        st.integers(0, 5),
        st.floats(0.05, 1.0),
        st.sampled_from([0, 0, 1, 2, 3, 5, 8, 11, 31]),
    ),
    min_size=1,
    max_size=14,
)


def _action(
    code: int, frac: float, evidence: tuple[EvidenceDocument, ...], amount: int
) -> AgentAction:
    match code:
        case 0:
            return Accept()
        case 1:
            k = max(1, round(frac * len(evidence)))
            return Represent(evidence_ids=tuple(d.doc_id for d in evidence[:k]))
        case 2:
            return Escalate()
        case 3:
            return RequestInfo()
        case 4:
            return Refund(amount_minor=max(1, int(amount * frac)))
        case _:
            return Represent(evidence_ids=tuple(d.doc_id for d in evidence))


def _start(seed: int, n: int, full: bool) -> tuple[DisputeEngine, ManualClock, list[CardDispute]]:
    cases = generate_cases(seed, n, FULL_MIX if full else DEFAULT_MIX)
    clock = ManualClock()
    clock.set_date(min(c.opened_on for c in cases))
    return DisputeEngine(cases, clock), clock, cases


def _run(
    seed: int, n: int, full: bool, plan: Plan
) -> tuple[DisputeEngine, list[CardDispute], list[tuple[int, str]]]:
    """Play a plan; record (event count, state digest) after every step."""
    e, clock, cases = _start(seed, n, full)
    marks = [(len(e.events), e.state_digest())]
    for idx, code, frac, days in plan:
        clock.advance(days=days)
        c = cases[idx % n]
        if days != 1 and today(clock) < c.opened_on:  # usually act once the case is open
            clock.set_date(c.opened_on)
        obs = e.observe(c.case_id)
        e.act(c.case_id, _action(code, frac, obs.evidence, c.amount_minor))
        marks.append((len(e.events), e.state_digest()))
    clock.advance(days=40)
    e.sync()
    marks.append((len(e.events), e.state_digest()))
    return e, cases, marks


class _NoIssuer:
    def merchant_prevails(self, *_: object) -> bool:
        raise AssertionError("a replay must not ask the issuer")


@settings(max_examples=40)
@given(st.integers(0, 2**32), st.integers(1, 8), st.booleans(), plans)
def test_replay_reproduces_state_exactly(seed: int, n: int, full: bool, plan: Plan) -> None:
    e, cases, _marks = _run(seed, n, full, plan)
    r = DisputeEngine.replay(cases, e.events, ManualClock(), issuer=_NoIssuer())
    assert r.state_digest() == e.state_digest()
    assert r.events == e.events
    assert r.ledger.entries == e.ledger.entries
    assert r.incidents == e.incidents
    for cid in e.case_ids:
        assert r.case_digest(cid) == e.case_digest(cid)
        assert r.outcome(cid) == e.outcome(cid)


@settings(max_examples=25)
@given(st.integers(0, 2**32), st.integers(1, 6), st.booleans(), plans)
def test_every_prefix_replays_to_the_state_at_that_point(
    seed: int, n: int, full: bool, plan: Plan
) -> None:
    e, cases, marks = _run(seed, n, full, plan)
    for k, digest in marks:
        assert DisputeEngine.replay(cases, e.events[:k], ManualClock()).state_digest() == digest


@settings(max_examples=25)
@given(st.integers(0, 2**32), st.integers(1, 6), plans)
def test_event_log_round_trips_through_json(seed: int, n: int, plan: Plan) -> None:
    e, cases, _ = _run(seed, n, True, plan)
    raw = e.events_json()
    back = DisputeEngine.events_from_json(raw)
    assert back == e.events
    assert DisputeEngine.replay(cases, back, ManualClock()).state_digest() == e.state_digest()


@settings(max_examples=20)
@given(st.integers(0, 2**32), st.integers(1, 6), plans)
def test_runs_are_deterministic(seed: int, n: int, plan: Plan) -> None:
    a, _, _ = _run(seed, n, True, plan)
    b, _, _ = _run(seed, n, True, plan)
    assert a.events_json() == b.events_json()
    assert a.state_digest() == b.state_digest()


@settings(max_examples=20)
@given(st.integers(0, 2**32), st.integers(1, 6), plans)
def test_ledger_balances_after_every_event(seed: int, n: int, plan: Plan) -> None:
    e, cases, _ = _run(seed, n, True, plan)
    for k in range(0, len(e.events) + 1, max(1, len(e.events) // 8)):
        r = DisputeEngine.replay(cases, e.events[:k], ManualClock())
        assert all(v == 0 for v in r.ledger.currency_totals().values())


def test_every_accepted_action_is_logged_and_refusals_are_not() -> None:
    e, _clock, cases = _start(5, 4, True)
    c = cases[0]
    n0 = len(e.events)
    bad = e.act(c.case_id, Escalate())  # not allowed at opening
    assert not bad.ok and len(e.events) == n0
    ok = e.act(c.case_id, RequestInfo())
    assert ok.ok
    last = e.events[-1]
    assert isinstance(last, ActionApplied) and last.action == RequestInfo()


def test_fold_starts_from_genesis() -> None:
    _, _, cases = _start(2, 5, True)
    empty = DisputeEngine.replay(cases, (), ManualClock())
    assert empty.events == () and empty.ledger.entries == ()
    assert all(empty._cases[c.case_id].life.stage is c.opening_stage for c in cases)


def test_construction_events_open_every_case() -> None:
    e, _, cases = _start(3, 6, True)
    captured = [ev.case_id for ev in e.events if isinstance(ev, CaseCaptured)]
    assert captured == [c.case_id for c in cases]


def test_fines_are_events_and_post_once() -> None:
    cases = generate_cases(4, 30, FULL_MIX)
    merchants = generate_merchants(4, FULL_MIX.n_merchants)
    clock = ManualClock()
    clock.set_date(min(c.opened_on for c in cases))
    e = DisputeEngine(cases, clock, merchants=merchants)
    e.post_monitoring_fines()
    n = len(e.events)
    e.post_monitoring_fines()
    assert len(e.events) == n
    r = DisputeEngine.replay(cases, e.events, ManualClock(), merchants=merchants)
    assert r.state_digest() == e.state_digest()
    r.post_monitoring_fines()  # the replayed engine knows the fines are posted
    assert len(r.events) == n


def test_full_drain_replays() -> None:
    e, clock, cases = _start(8, 40, True)
    drain(e, clock)
    r = DisputeEngine.replay(cases, e.events, ManualClock())
    assert r.state_digest() == e.state_digest()
    assert not r.open_cases()


def test_case_digest_ignores_other_cases() -> None:
    _, _, cases = _start(6, 3, False)
    a = DisputeEngine(cases, ManualClock())
    b = DisputeEngine(cases[1:], ManualClock())
    assert a.case_digest(cases[1].case_id) == b.case_digest(cases[1].case_id)
    assert a.state_digest() != b.state_digest()


def test_bad_logs_are_refused() -> None:
    e, _, cases = _start(1, 3, False)
    other = generate_cases(99, 1)
    with pytest.raises(ReplayError, match="unknown case"):
        DisputeEngine.replay(other, e.events, ManualClock())
    stray: EngineEvent = StageFired(
        case_id=cases[0].case_id, event=Event.RULING, on=cases[0].opened_on
    )
    with pytest.raises(ReplayError, match="does not fit"):
        DisputeEngine.replay(cases, (stray,), ManualClock())
    failed = DisputeEngine.replay(cases, (), ManualClock())
    with pytest.raises(ReplayError):
        failed._emit(stray)
    assert failed.events == ()  # a refused event is not recorded


def test_a_busy_run_uses_the_main_event_types() -> None:
    e, clock, cases = _start(11, 60, True)
    for c in sorted(cases, key=lambda x: x.opened_on)[:30]:
        clock.set_date(max(c.opened_on, today(clock)))
        obs = e.observe(c.case_id)
        e.act(c.case_id, Represent(evidence_ids=tuple(d.doc_id for d in obs.evidence)))
    drain(e, clock)
    kinds = {ev.type for ev in e.events}
    assert {
        "captured",
        "chargeback_debited",
        "fee_charged",
        "stage_fired",
        "awaiting_set",
        "evidence_submitted",
        "case_resolved",
        "action_applied",
    } <= kinds
    assert any(isinstance(ev, StageFired) and ev.on >= c.opened_on for ev in e.events)
    assert Stage.RESOLVED in {e._cases[c.case_id].life.stage for c in cases}
