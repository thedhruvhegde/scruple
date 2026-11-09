"""Automatic one-step rollback when a canary ramp breaches."""

from __future__ import annotations

from typing import Any

import pytest

from scruple.clock import SteppingClock
from scruple.gate.canary import Arm, RampSchedule, evaluate_stage
from scruple.gate.config import GateConfig
from scruple.gate.models import Outcome, Stage
from scruple.registry import InvalidTransitionError, RuleStatus
from scruple.service import AgentGate, ApprovalError
from scruple.sim.generator import Profile, generate_cases
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.sim.models import DisputeCase
from scruple.storage import SQLiteStorage
from tests.ci_support import APPROVER, PROV

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300)
SCHEDULE = RampSchedule(stages=(5.0, 10.0, 25.0, 50.0))
# Synthetic drift: delivered-but-"not received" claims become mostly valid, so
# contesting them now loses in arbitration.
DRIFT = Profile(name="drift", p_valid_inr_delivered=0.75)


def _live(stage: int, drift: bool = False) -> list[DisputeCase]:
    return generate_cases(700 + stage, 2_000, DRIFT if drift else Profile(), prefix=f"l{stage}-")


def _ramping() -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    return g


@pytest.mark.parametrize("breach_at", [0, 1, 2, 3])
def test_breach_mid_ramp_rolls_back_in_one_step(breach_at: int) -> None:
    g = _ramping()
    active_before = [r.ref for r in g.registry.active_rules()]
    for i in range(breach_at):
        _, state = g.advance_canary(GOOD_RULE_ID, 1, _live(i))
        assert state.status == "ramping"
    n_before = len(g.audit.entries())
    rec, state = g.advance_canary(GOOD_RULE_ID, 1, _live(breach_at, drift=True))
    assert rec.decision.outcome is Outcome.ROLLBACK and rec.decision.stage is Stage.CANARY
    assert state.status == "breached" and state.percent == 0.0
    assert state.stage_index == breach_at
    # Exactly one decision, one breach marker and one status change, in that order.
    new = g.audit.entries()[n_before:]
    assert [e.event_type for e in new] == [
        "decision.recorded",
        "canary.breached",
        "rule.status_changed",
    ]
    change = new[-1].payload
    assert change["to_status"] == "rejected" and change["auto_rollback"] is True
    assert change["decision_id"] == rec.decision.decision_id
    assert change["percent"] == SCHEDULE.stages[breach_at]
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.REJECTED
    assert [r.ref for r in g.registry.active_rules()] == active_before
    assert g.verify_integrity().ok


def test_after_breach_all_traffic_is_baseline_and_ramp_is_closed() -> None:
    g = _ramping()
    g.advance_canary(GOOD_RULE_ID, 1, _live(0))
    g.advance_canary(GOOD_RULE_ID, 1, _live(1, drift=True))
    assert all(g.canary_arm(GOOD_RULE_ID, 1, f"c{i}") is Arm.BASELINE for i in range(2_000))
    with pytest.raises(InvalidTransitionError):
        g.advance_canary(GOOD_RULE_ID, 1, _live(2))
    with pytest.raises(ApprovalError):
        g.approve(GOOD_RULE_ID, 1, APPROVER)
    with pytest.raises(InvalidTransitionError):
        g.evaluate(GOOD_RULE_ID, 1)  # rejected versions are never re-evaluated
    with pytest.raises(InvalidTransitionError):
        g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)


def test_breach_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the status change fails, neither the decision nor the breach marker stays."""
    g = _ramping()
    head, n_dec = g.audit.head(), len(g.decisions())

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("storage down")

    monkeypatch.setattr(g.registry, "transition", boom)
    with pytest.raises(RuntimeError):
        g.advance_canary(GOOD_RULE_ID, 1, _live(0, drift=True))
    assert g.audit.head() == head and len(g.decisions()) == n_dec
    state = g.canary_state(GOOD_RULE_ID, 1)
    assert state is not None and state.status == "ramping"
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.CANARY
    assert g.verify_integrity().ok


def test_hard_violations_breach_even_when_cost_looks_fine() -> None:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    rv = g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    res = evaluate_stage(rv, g.registry.active_rules(), _live(5), SCHEDULE, 3)
    assert res.outcome == "breach" and res.hard_findings
    assert any(c.name == "canary_no_hard_violations" and not c.passed for c in res.checks)


def test_manual_kill_switch() -> None:
    g = _ramping()
    with pytest.raises(ApprovalError, match="not an authorised"):
        g.breach_canary(GOOD_RULE_ID, 1, "random-person", "looks odd")
    rec = g.breach_canary(GOOD_RULE_ID, 1, APPROVER, "partner alert on disputes queue")
    assert rec.decision.outcome is Outcome.ROLLBACK and rec.decision.actor == APPROVER
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.REJECTED
    breach = next(e for e in g.audit.entries() if e.event_type == "canary.breached")
    assert breach.payload["manual"] is True and breach.actor == APPROVER
    with pytest.raises(InvalidTransitionError, match="no ramp"):
        g.breach_canary(GOOD_RULE_ID, 1, APPROVER, "again")
    assert g.verify_integrity().ok


def test_breach_report_explains_itself() -> None:
    g = _ramping()
    rec, _ = g.advance_canary(GOOD_RULE_ID, 1, _live(0, drift=True))
    md = g.report(rec.decision.decision_id) or ""
    assert "AUTOMATICALLY ROLLED BACK" in md and "canary_cost_within_tolerance" in md


def test_tampering_with_breach_status_is_detected() -> None:
    g = _ramping()
    g.advance_canary(GOOD_RULE_ID, 1, _live(0, drift=True))
    # Raw edit, as someone with database access might: flip the projection back.
    g.storage.update_rule_status(GOOD_RULE_ID, 1, RuleStatus.CANARY)
    rep = g.verify_integrity()
    assert not rep.ok and any("audit log says rejected" in p for p in rep.problems)
