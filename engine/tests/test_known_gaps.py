"""Known verifier weaknesses, pinned as tests so they stay visible.

Each test asserts that a weakness still exists. When one is fixed, its test fails:
update docs/VERIFIER_FINDINGS.md and move the test to test_adversarial.py.
"""

from __future__ import annotations

from agent_gate.domain import Action, RuleSpec
from agent_gate.gate import GateConfig, Outcome, evaluate_shadow
from agent_gate.registry import Provenance, Registry
from agent_gate.sim.generator import Profile
from agent_gate.sim.library import GOOD_RULE, GOOD_RULE_ID, C
from tests.helpers import baseline_versions, rv

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=500)


def test_gap_adaptive_resubmission_is_not_rate_limited(registry: Registry) -> None:
    """G-1: an agent may submit many behaviourally different variants against the same
    held-out set and keep whichever passes (adaptive overfitting). No budget exists."""
    prov = Provenance(agent_id="agent-7")
    for threshold in range(1_000, 21_000, 1_000):
        spec = RuleSpec(
            description="variant",
            action=Action.CONTEST,
            priority=50,
            conditions=(
                C("reason_code", "eq", "not_received"),
                C("amount_usd_minor", "gt", threshold),
            ),
        )
        registry.register_candidate("probe", spec, owner="ops", provenance=prov)
    assert len(registry.versions("probe")) == 20


def test_gap_shadow_replay_trusts_history_under_drift() -> None:
    """G-2: shadow replay only knows the historical distribution. A rule that is good
    on history and bad under live drift still passes shadow; only canary catches it."""
    drift = Profile(name="drift", p_valid_inr_delivered=0.75)
    cfg = CFG.model_copy(update={"profile": Profile()})
    assert (
        evaluate_shadow(rv(GOOD_RULE_ID, GOOD_RULE), baseline_versions(), cfg).outcome
        is Outcome.PROMOTE
    )
    drifted = CFG.model_copy(update={"profile": drift})
    assert (
        evaluate_shadow(rv(GOOD_RULE_ID, GOOD_RULE), baseline_versions(), drifted).outcome
        is not Outcome.PROMOTE
    )
