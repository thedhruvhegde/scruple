"""End to end, synthetic: a candidate PR is blocked, the fixed candidate passes,
the canary ramps, breaches under drift and rolls back; the evidence pack shows it."""

from __future__ import annotations

from pathlib import Path

from scruple.ci.config import CandidateFile
from scruple.ci.evidence import build_evidence_pack, load_pack
from scruple.ci.scorecard import Verdict, load_scorecard
from scruple.cli.main import main
from scruple.clock import SteppingClock
from scruple.gate.canary import Arm, RampSchedule
from scruple.gate.config import GateConfig
from scruple.gate.models import Outcome
from scruple.registry import RuleStatus
from scruple.service import AgentGate
from scruple.sim.generator import Profile, generate_cases
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE
from tests.ci_support import PROV, write_repo

DRIFT = Profile(name="drift", p_valid_inr_delivered=0.75)


def test_blocked_then_fixed_then_ramp_breach_rollback(tmp_path: Path) -> None:
    db = tmp_path / "gate.db"

    # 1. The pull request proposes the subtly bad rule: the gate blocks it.
    pr1 = write_repo(
        tmp_path / "pr1",
        CandidateFile(rule_id=BAD_RULE_ID, spec=BAD_RULE, owner="ops-team", provenance=PROV),
    )
    card1 = tmp_path / "pr1.json"
    assert main(["ci", "run", "--config", str(pr1), "--db", str(db), "--out", str(card1)]) == 1
    blocked = load_scorecard(card1.read_text())
    assert blocked.verdict is Verdict.BLOCK and blocked.version == 1
    assert {r.name for r in blocked.regressions} >= {"no_new_policy_violations", "late_evidence"}

    # 2. The author fixes precedence (priority 50, below the late-evidence safeguard)
    #    and pushes. Same rule id, new version: the check passes.
    fixed = CandidateFile(rule_id=BAD_RULE_ID, spec=GOOD_RULE, owner="ops-team", provenance=PROV)
    pr2 = write_repo(tmp_path / "pr2", fixed)
    card2 = tmp_path / "pr2.json"
    assert main(["ci", "run", "--config", str(pr2), "--db", str(db), "--out", str(card2)]) == 0
    passed = load_scorecard(card2.read_text())
    assert passed.verdict is Verdict.PASS and passed.version == 2 and not passed.regressions

    # 3. After merge, the same gate database drives the staged canary.
    g = AgentGate.open(str(db), SteppingClock(), GateConfig(n_cases=2_000, bootstrap_resamples=500))
    assert g.registry.get(BAD_RULE_ID, 1).status is RuleStatus.REJECTED
    assert g.registry.get(BAD_RULE_ID, 2).status is RuleStatus.CANARY
    active_before = [r.ref for r in g.registry.active_rules()]
    g.start_canary_ramp(BAD_RULE_ID, 2, RampSchedule(stages=(10.0, 25.0, 50.0, 75.0)))
    rec, state = g.advance_canary(BAD_RULE_ID, 2, generate_cases(901, 4_000, prefix="w1-"))
    assert rec.decision.outcome is Outcome.PROMOTE and state.percent == 25.0
    rec, state = g.advance_canary(BAD_RULE_ID, 2, generate_cases(902, 4_000, prefix="w2-"))
    assert rec.decision.outcome is Outcome.PROMOTE and state.percent == 50.0

    # 4. Mid-ramp (50%), live traffic drifts: delivered "not received" claims turn valid.
    rec, state = g.advance_canary(BAD_RULE_ID, 2, generate_cases(903, 2_000, DRIFT, prefix="w3-"))
    assert rec.decision.outcome is Outcome.ROLLBACK and state.status == "breached"

    # 5. Rolled back in one step: rejected, no traffic, live policy unchanged, chain intact.
    assert g.registry.get(BAD_RULE_ID, 2).status is RuleStatus.REJECTED
    assert [r.ref for r in g.registry.active_rules()] == active_before
    assert all(g.canary_arm(BAD_RULE_ID, 2, f"x{i}") is Arm.BASELINE for i in range(500))
    assert g.verify_integrity().ok
    assert main(["audit", "verify", "--db", str(db)]) == 0

    # 6. The evidence pack tells the whole story and its proof checks out.
    out = tmp_path / "evidence"
    assert main(["ci", "evidence", "--db", str(db), BAD_RULE_ID, "2", "--out", str(out)]) == 0
    pack, claimed = load_pack((out / f"evidence-{BAD_RULE_ID}-v2.json").read_text())
    assert pack.pack_sha256 == claimed and pack.audit.proof.ok
    assert pack.status == "rejected" and pack.scorecard.verdict is Verdict.BLOCK
    assert pack.canary_ramp is not None and pack.canary_ramp.status == "breached"
    events = [e.event_type for e in pack.audit.entries]
    assert events[-3:] == ["decision.recorded", "canary.breached", "rule.status_changed"]
    assert pack == build_evidence_pack(g, BAD_RULE_ID, 2).model_copy(
        update={"config_sha256": pack.config_sha256, "scorecard": pack.scorecard}
    )
