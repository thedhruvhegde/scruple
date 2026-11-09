from __future__ import annotations

import pytest

from agent_gate.clock import SteppingClock
from agent_gate.gate import GateConfig, Outcome
from agent_gate.gate.models import Stage
from agent_gate.registry import InvalidTransitionError, Provenance, RuleStatus
from agent_gate.service import AgentGate, ApprovalError
from agent_gate.sim.generator import Profile
from agent_gate.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from agent_gate.storage import SQLiteStorage

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=500)
APPROVER = "model-risk@demo.bank"
PROV = Provenance(agent_id="dispute-agent-1", episode_ids=("ep-1", "ep-2", "ep-3"))


@pytest.fixture
def gate() -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    return g


def _promote_good(gate: AgentGate) -> None:
    gate.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops-team", provenance=PROV)
    recs = gate.evaluate(GOOD_RULE_ID, 1)
    assert [r.decision.outcome for r in recs] == [Outcome.PROMOTE, Outcome.CANARY_PASSED]
    gate.approve(GOOD_RULE_ID, 1, APPROVER)


def test_good_rule_end_to_end(gate: AgentGate) -> None:
    _promote_good(gate)
    assert gate.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.ACTIVE
    assert gate.verify_integrity().ok


def test_bad_rule_is_blocked_and_rejected(gate: AgentGate) -> None:
    gate.propose(BAD_RULE_ID, BAD_RULE, owner="ops-team", provenance=PROV)
    recs = gate.evaluate(BAD_RULE_ID, 1)
    assert [r.decision.outcome for r in recs] == [Outcome.BLOCK]
    assert gate.registry.get(BAD_RULE_ID, 1).status is RuleStatus.REJECTED
    with pytest.raises(InvalidTransitionError):
        gate.evaluate(BAD_RULE_ID, 1)  # cannot be re-evaluated


def test_cannot_approve_without_canary_pass(gate: AgentGate) -> None:
    gate.propose(BAD_RULE_ID, BAD_RULE, owner="ops-team", provenance=PROV)
    gate.evaluate(BAD_RULE_ID, 1)
    with pytest.raises(ApprovalError):
        gate.approve(BAD_RULE_ID, 1, APPROVER)


def test_unauthorised_and_self_approval_refused(gate: AgentGate) -> None:
    cfg = CFG.model_copy(update={"approvers": (APPROVER, "ops-team")})
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), cfg)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops-team", provenance=PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    with pytest.raises(ApprovalError, match="not an authorised"):
        g.approve(GOOD_RULE_ID, 1, "random-person")
    with pytest.raises(ApprovalError, match="segregation"):
        g.approve(GOOD_RULE_ID, 1, "ops-team")


def test_rollback_restores_previous_policy(gate: AgentGate) -> None:
    _promote_good(gate)
    before = [r.ref for r in gate.registry.active_rules()]
    rec = gate.rollback(GOOD_RULE_ID, APPROVER, "demo drill")
    assert rec.decision.outcome is Outcome.ROLLED_BACK
    after = [r.ref for r in gate.registry.active_rules()]
    assert f"{GOOD_RULE_ID}@v1" in before and f"{GOOD_RULE_ID}@v1" not in after
    with pytest.raises(ApprovalError):
        gate.rollback("base-fx-contest", "random-person", "nope")
    assert gate.verify_integrity().ok


def test_canary_drift_auto_rolls_back(gate: AgentGate) -> None:
    drift = Profile(name="drift", p_valid_inr_delivered=0.75)
    g = AgentGate(
        SQLiteStorage(":memory:"), SteppingClock(), CFG.model_copy(update={"canary_profile": drift})
    )
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops-team", provenance=PROV)
    recs = g.evaluate(GOOD_RULE_ID, 1)
    assert recs[-1].decision.outcome is Outcome.ROLLBACK
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.REJECTED
    assert g.verify_integrity().ok


def test_assurance_healthy_then_rolls_back_regression(gate: AgentGate) -> None:
    _promote_good(gate)
    rec = gate.run_assurance()
    assert rec is not None and rec.decision.outcome is Outcome.HEALTHY
    # A bad rule reaches production by bypassing the gate (forced via the registry,
    # as a broken process might). Continuous assurance must catch and revert it.
    gate.propose(BAD_RULE_ID, BAD_RULE, owner="ops-team", provenance=PROV)
    for s in (RuleStatus.SHADOW, RuleStatus.CANARY):
        gate.registry.transition(BAD_RULE_ID, 1, s, "admin", "forced, bypassing the gate")
    gate.registry.activate(BAD_RULE_ID, 1, "admin", "forced, bypassing the gate")
    rec = gate.run_assurance()
    assert rec is not None and rec.decision.outcome is Outcome.ROLLBACK
    assert gate.registry.active_version(BAD_RULE_ID) is None
    assert gate.registry.active_version(GOOD_RULE_ID) is not None
    assert [d.decision.stage for d in gate.decisions(BAD_RULE_ID)] == [
        Stage.ASSURANCE,
        Stage.ROLLBACK,
    ]


def test_every_decision_has_a_readable_report(gate: AgentGate) -> None:
    _promote_good(gate)
    gate.propose(BAD_RULE_ID, BAD_RULE, owner="ops-team", provenance=PROV)
    gate.evaluate(BAD_RULE_ID, 1)
    for rec in gate.decisions():
        md = gate.report(rec.decision.decision_id)
        assert md is not None
        assert md.startswith(f"# Evidence report {rec.decision.decision_id}")
        assert "Who can approve or roll back" in md and "Audit anchor" in md
    blocked = gate.decisions(BAD_RULE_ID)[0]
    md = gate.report(blocked.decision.decision_id) or ""
    assert "BLOCKED" in md and "**WORSE**" in md and "base-late-evidence-accept@v1" in md


def test_decisions_are_reproducible(tmp_path: object) -> None:
    def run() -> list[str]:
        g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
        g.bootstrap_baseline()
        g.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops-team", provenance=PROV)
        g.evaluate(GOOD_RULE_ID, 1)
        return [d.decision_sha256 for d in g.decisions()] + [g.audit.head()]

    assert run() == run()
