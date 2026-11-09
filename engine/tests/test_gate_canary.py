"""Canary controller: assignment properties, ramp schedule, persisted and audited ramp."""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.clock import SteppingClock
from scruple.gate.canary import (
    BUCKETS,
    CANARY_EVENTS,
    Arm,
    RampSchedule,
    assign,
    basis_points,
    bucket,
    evaluate_stage,
    project_state,
)
from scruple.gate.config import GateConfig
from scruple.gate.engine import in_canary
from scruple.gate.models import Outcome, Stage
from scruple.registry import InvalidTransitionError, RuleStatus
from scruple.service import AgentGate
from scruple.sim.generator import generate_cases
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300)
SCHEDULE = RampSchedule(stages=(5.0, 10.0, 25.0, 50.0))
ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=40)
salts = st.text(alphabet="0123456789abcdef", min_size=8, max_size=64)
percents = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)


# --- assignment properties -----------------------------------------------------------


@given(ids, salts, percents)
def test_assignment_is_stable(case_id: str, salt: str, p: float) -> None:
    assert assign(case_id, salt, p) is assign(case_id, salt, p)


@given(ids, salts, percents, percents)
def test_assignment_is_monotone_in_percent(case_id: str, salt: str, a: float, b: float) -> None:
    lo, hi = sorted((a, b))
    if assign(case_id, salt, lo) is Arm.CANDIDATE:
        assert assign(case_id, salt, hi) is Arm.CANDIDATE


@given(ids, salts)
def test_endpoints(case_id: str, salt: str) -> None:
    assert assign(case_id, salt, 0.0) is Arm.BASELINE
    assert assign(case_id, salt, 100.0) is Arm.CANDIDATE
    assert 0 <= bucket(case_id, salt) < BUCKETS


@given(ids, salts, st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_agrees_with_one_shot_canary(case_id: str, salt: str, fraction: float) -> None:
    """Same bucket formula as engine.in_canary, so the two never disagree."""
    expected = in_canary(case_id, salt, fraction)
    if int(fraction * 10_000) == basis_points(fraction * 100):
        assert (assign(case_id, salt, fraction * 100) is Arm.CANDIDATE) == expected


@settings(max_examples=25, deadline=None)
@given(salts, st.floats(min_value=0.5, max_value=99.5, allow_nan=False), st.text(max_size=5))
def test_proportion_within_bound_for_large_n(salt: str, p: float, prefix: str) -> None:
    n = 20_000
    hits = sum(assign(f"{prefix}case-{i}", salt, p) is Arm.CANDIDATE for i in range(n))
    q = basis_points(p) / BUCKETS
    sigma = math.sqrt(q * (1 - q) / n)
    # 6 sigma plus bucket rounding: a SHA-256 split should never be this far off.
    assert abs(hits / n - q) <= 6 * sigma + 1 / BUCKETS


@settings(max_examples=10, deadline=None)
@given(salts, salts)
def test_different_salts_give_independent_slices(s1: str, s2: str) -> None:
    if s1 == s2:
        return
    n, p = 10_000, 20.0
    a = {i for i in range(n) if assign(f"c{i}", s1, p) is Arm.CANDIDATE}
    b = {i for i in range(n) if assign(f"c{i}", s2, p) is Arm.CANDIDATE}
    # Independent 20% slices overlap about 4%; identical ones would overlap 20%.
    assert len(a & b) / n < 0.08


@pytest.mark.parametrize("bad", [-0.1, 100.1, math.inf])
def test_percent_bounds(bad: float) -> None:
    with pytest.raises(ValueError, match="percent"):
        assign("c", "s", bad)


# --- schedule --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stages",
    [(), (0.0, 10.0), (10.0, 100.0), (10.0, 5.0), (10.0, 10.0), (5.0, 5.001), (-1.0,)],
)
def test_schedule_validation(stages: tuple[float, ...]) -> None:
    with pytest.raises(ValidationError):
        RampSchedule(stages=stages)


@given(st.lists(st.integers(min_value=1, max_value=9_999), min_size=1, max_size=8, unique=True))
def test_any_increasing_schedule_is_valid(bps: list[int]) -> None:
    stages = tuple(b / 100 for b in sorted(bps))
    assert RampSchedule(stages=stages).stages == stages


# --- stage evaluation ------------------------------------------------------------------


def _gate(rule_id: str = GOOD_RULE_ID) -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    g.propose(rule_id, GOOD_RULE if rule_id == GOOD_RULE_ID else BAD_RULE, "ops-team", PROV)
    g.evaluate(rule_id, 1)
    return g


def _live(stage: int, n: int = 2_000) -> list:  # type: ignore[type-arg]
    return generate_cases(500 + stage, n, prefix=f"live{stage}-")


def test_stage_evaluation_is_pure_and_deterministic() -> None:
    g = _gate()
    rv = g.registry.get(GOOD_RULE_ID, 1)
    active = [r for r in g.registry.active_rules() if r.rule_id != GOOD_RULE_ID]
    head = g.audit.head()
    a = evaluate_stage(rv, active, _live(0), SCHEDULE, 1)
    b = evaluate_stage(rv, active, _live(0), SCHEDULE, 1)
    assert a == b and a.outcome == "advance" and a.percent == 10.0
    assert g.audit.head() == head
    with pytest.raises(ValueError, match="outside"):
        evaluate_stage(rv, active, _live(0), SCHEDULE, 4)
    with pytest.raises(ValueError, match="live cases"):
        evaluate_stage(rv, active, [], SCHEDULE, 0)


def test_treatment_share_tracks_stage_percent() -> None:
    g = _gate()
    rv = g.registry.get(GOOD_RULE_ID, 1)
    active = [r for r in g.registry.active_rules() if r.rule_id != GOOD_RULE_ID]
    cases = _live(1, 4_000)
    shares = [
        evaluate_stage(rv, active, cases, SCHEDULE, i).treatment_cases / len(cases)
        for i in range(4)
    ]
    assert shares == sorted(shares)
    for share, p in zip(shares, SCHEDULE.stages, strict=True):
        assert abs(share - p / 100) < 0.03


def test_too_little_traffic_holds() -> None:
    g = _gate()
    rv = g.registry.get(GOOD_RULE_ID, 1)
    res = evaluate_stage(rv, g.registry.active_rules(), _live(2, 150), SCHEDULE, 0)
    assert res.outcome == "hold" and res.gate_outcome is Outcome.HOLD


# --- ramp through the service ----------------------------------------------------------


def test_full_ramp_persists_in_audit_and_enables_approval() -> None:
    g = _gate()
    state = g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    assert state.status == "ramping" and state.percent == 5.0
    assert g.verify_integrity().ok
    seen = []
    for i in range(len(SCHEDULE.stages)):
        rec, state = g.advance_canary(GOOD_RULE_ID, 1, _live(i))
        seen.append((rec.decision.outcome, state.status, state.percent))
        assert rec.decision.stage is Stage.CANARY
        assert g.verify_integrity().ok
    assert seen == [
        (Outcome.PROMOTE, "ramping", 10.0),
        (Outcome.PROMOTE, "ramping", 25.0),
        (Outcome.PROMOTE, "ramping", 50.0),
        (Outcome.CANARY_PASSED, "completed", 50.0),
    ]
    events = [e.event_type for e in g.audit.entries() if e.event_type in CANARY_EVENTS]
    assert events == [
        "canary.ramp_started",
        *["canary.stage_passed"] * 3,
        "canary.ramp_completed",
    ]
    # Rebuilt from the chain alone, e.g. after a restart.
    assert project_state(g.audit.entries(), GOOD_RULE_ID, 1) == state
    with pytest.raises(InvalidTransitionError, match="no ramp in progress"):
        g.advance_canary(GOOD_RULE_ID, 1, _live(9))
    g.approve(GOOD_RULE_ID, 1, "model-risk@demo.bank")
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.ACTIVE


def test_cannot_approve_mid_ramp() -> None:
    from scruple.service import ApprovalError

    g = _gate()
    g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    g.advance_canary(GOOD_RULE_ID, 1, _live(0))
    with pytest.raises(ApprovalError, match="not passed canary"):
        g.approve(GOOD_RULE_ID, 1, "model-risk@demo.bank")


def test_ramp_survives_reopening_the_database(tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    db = str(tmp_path / "g.db")
    g = AgentGate.open(db, SteppingClock(), CFG)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    g.advance_canary(GOOD_RULE_ID, 1, _live(0))
    g2 = AgentGate.open(db, SteppingClock(), CFG)
    state = g2.canary_state(GOOD_RULE_ID, 1)
    assert state is not None and state.stage_index == 1 and state.percent == 10.0


def test_hold_keeps_stage_and_is_audited() -> None:
    g = _gate()
    g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    rec, state = g.advance_canary(GOOD_RULE_ID, 1, _live(0, 150))
    assert rec.decision.outcome is Outcome.HOLD and state.stage_index == 0
    assert g.audit.entries()[-1].event_type == "canary.stage_held"
    assert g.verify_integrity().ok


def test_start_requires_passed_canary_and_only_once() -> None:
    g = _gate(BAD_RULE_ID)  # blocked in shadow
    with pytest.raises(InvalidTransitionError, match="not passed canary"):
        g.start_canary_ramp(BAD_RULE_ID, 1, SCHEDULE)
    g = _gate()
    g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    with pytest.raises(InvalidTransitionError, match="already"):
        g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    with pytest.raises(InvalidTransitionError, match="no ramp"):
        g.advance_canary(BAD_RULE_ID, 1)


def test_default_batches_are_seeded() -> None:
    def run() -> list[str]:
        g = _gate()
        g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
        g.advance_canary(GOOD_RULE_ID, 1)
        return [d.decision_sha256 for d in g.decisions()] + [g.audit.head()]

    assert run() == run()


def test_canary_arm_routes_by_current_stage() -> None:
    g = _gate()
    case_ids = [f"live-{i}" for i in range(4_000)]
    assert all(g.canary_arm(GOOD_RULE_ID, 1, c) is Arm.BASELINE for c in case_ids)
    g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    at5 = {c for c in case_ids if g.canary_arm(GOOD_RULE_ID, 1, c) is Arm.CANDIDATE}
    g.advance_canary(GOOD_RULE_ID, 1, _live(0))
    at10 = {c for c in case_ids if g.canary_arm(GOOD_RULE_ID, 1, c) is Arm.CANDIDATE}
    assert at5 < at10  # monotone: nobody who had the candidate loses it mid-ramp
    assert abs(len(at10) / len(case_ids) - 0.10) < 0.02
