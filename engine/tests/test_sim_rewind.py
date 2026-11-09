"""Rewind (037): restore one case to any event index; other cases are untouched."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.sim.deadlines import ManualClock, today
from scruple.sim.disputes import (
    FULL_MIX,
    Accept,
    ActionApplied,
    CardDispute,
    DisputeEngine,
    EngineEvent,
    Represent,
    RequestInfo,
    drain,
    generate_cases,
)
from scruple.sim.models import Resolution, Stage
from tests.test_sim_events import Plan, _action, plans


class _Recording(DisputeEngine):
    """Records each case's digest after each of its events (the oracle for rewind)."""

    trace: dict[str, list[str]]

    def _emit(self, ev: EngineEvent) -> None:
        super()._emit(ev)
        if not hasattr(self, "trace"):
            self.trace = {}
        if ev.case_id is not None:
            self.trace.setdefault(ev.case_id, []).append(self.case_digest(ev.case_id))


def _play(seed: int, n: int, plan: Plan) -> tuple[_Recording, ManualClock, list[CardDispute]]:
    cases = generate_cases(seed, n, FULL_MIX)
    clock = ManualClock()
    clock.set_date(min(c.opened_on for c in cases))
    e = _Recording(cases, clock)
    for idx, code, frac, days in plan:
        clock.advance(days=days)
        c = cases[idx % n]
        if today(clock) < c.opened_on:
            clock.set_date(c.opened_on)
        obs = e.observe(c.case_id)
        e.act(c.case_id, _action(code, frac, obs.evidence, c.amount_minor))
    clock.advance(days=45)
    e.sync()
    return e, clock, cases


@settings(max_examples=30)
@given(st.integers(0, 2**32), st.integers(1, 5), plans, st.data())
def test_rewind_restores_the_case_state_at_that_event(
    seed: int, n: int, plan: Plan, data: st.DataObject
) -> None:
    e, _, cases = _play(seed, n, plan)
    c = data.draw(st.sampled_from(cases))
    trace = list(e.trace[c.case_id])
    lo = e.opening_events(c.case_id)
    k = data.draw(st.integers(lo, len(trace)))
    others = {x.case_id: e.case_digest(x.case_id) for x in cases if x.case_id != c.case_id}
    removed = e.rewind(c.case_id, k)
    assert len(removed) == len(trace) - k
    assert e.case_digest(c.case_id) == trace[k - 1]
    assert {cid: e.case_digest(cid) for cid in others} == others
    assert len(e.case_events(c.case_id)) == k
    # the rewound log is itself a valid log: replay gives the same state
    r = DisputeEngine.replay(cases, e.events, ManualClock())
    assert r.state_digest() == e.state_digest()


@settings(max_examples=25)
@given(st.integers(0, 2**32), st.integers(1, 5), plans, st.data())
def test_rewind_then_redo_gets_back_to_the_same_case_state(
    seed: int, n: int, plan: Plan, data: st.DataObject
) -> None:
    e, _, cases = _play(seed, n, plan)
    c = data.draw(st.sampled_from(cases))
    before = e.case_digest(c.case_id)
    k = data.draw(st.integers(e.opening_events(c.case_id), len(e.case_events(c.case_id))))
    for ev in e.rewind(c.case_id, k):
        e._emit(ev)
    assert e.case_digest(c.case_id) == before


@settings(max_examples=25)
@given(st.integers(0, 2**32), st.integers(1, 5), plans)
def test_at_index_matches_the_prefix_and_leaves_the_engine_alone(
    seed: int, n: int, plan: Plan
) -> None:
    e, _, cases = _play(seed, n, plan)
    whole = e.state_digest()
    for k in (0, len(e.events) // 2, len(e.events)):
        assert (
            e.at(k).state_digest()
            == DisputeEngine.replay(cases, e.events[:k], ManualClock()).state_digest()
        )
    assert e.state_digest() == whole
    assert e.at(len(e.events)).state_digest() == whole


def test_rewind_to_now_is_a_no_op() -> None:
    e, _, cases = _play(3, 4, [(0, 5, 1.0, 1), (1, 0, 1.0, 2)])
    d = e.state_digest()
    for c in cases:
        assert e.rewind(c.case_id, len(e.case_events(c.case_id))) == ()
    assert e.state_digest() == d


def test_rewind_bounds() -> None:
    e, _, cases = _play(4, 2, [(0, 5, 1.0, 1)])
    c = cases[0]
    lo = e.opening_events(c.case_id)
    assert lo >= 1
    with pytest.raises(ValueError, match="can rewind"):
        e.rewind(c.case_id, lo - 1)
    with pytest.raises(ValueError, match="can rewind"):
        e.rewind(c.case_id, len(e.case_events(c.case_id)) + 1)
    with pytest.raises(KeyError):
        e.rewind("nope", 0)
    with pytest.raises(ValueError, match="event index"):
        e.at(len(e.events) + 1)


def _chargeback_case() -> tuple[DisputeEngine, ManualClock, CardDispute]:
    cases = [c for c in generate_cases(12, 80, FULL_MIX) if c.opening_stage is Stage.CHARGEBACK]
    case = cases[0]
    clock = ManualClock()
    clock.set_date(case.opened_on)
    return DisputeEngine([case], clock), clock, case


def test_rewind_lets_an_agent_try_a_different_action() -> None:
    e, _clock, case = _chargeback_case()
    e.act(case.case_id, RequestInfo())
    assert e.act(case.case_id, Accept()).ok
    assert e.outcome(case.case_id) is not None
    fresh = ManualClock()
    fresh.set_date(case.opened_on)
    day = e.rewind_before_action(case.case_id, 1, clock=fresh)
    assert day == case.opened_on
    assert not e.is_resolved(case.case_id)
    assert e.allowed_actions(case.case_id)[0] == "accept"
    obs = e.observe(case.case_id)
    res = e.act(case.case_id, Represent(evidence_ids=tuple(d.doc_id for d in obs.evidence)))
    assert res.ok and res.stage_after is not Stage.CHARGEBACK
    drain(e, fresh)
    out = e.outcome(case.case_id)
    assert out is not None and out.resolution is not Resolution.ACCEPTED
    acts = [ev for _, ev in e.case_events(case.case_id) if isinstance(ev, ActionApplied)]
    assert [a.action.kind for a in acts] == ["request_info", "represent"]


def test_rewind_before_action_bounds() -> None:
    e, _, case = _chargeback_case()
    with pytest.raises(ValueError, match="no action 0"):
        e.rewind_before_action(case.case_id, 0)


def test_rewind_keeps_the_clock_unless_given_one() -> None:
    e, clock, case = _chargeback_case()
    e.act(case.case_id, Accept())
    clock.advance(days=100)
    e.rewind_before_action(case.case_id, 0)
    assert e.clock is clock
    e.sync()  # 100 days later the chargeback window has passed: the case is lost
    out = e.outcome(case.case_id)
    assert out is not None and out.resolution is Resolution.LOST
