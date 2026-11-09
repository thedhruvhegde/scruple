from __future__ import annotations

from scruple.domain import Action, RuleSpec
from scruple.gate import GateConfig, Outcome, evaluate_canary, evaluate_shadow
from scruple.gate.stats import paired_bootstrap_ci
from scruple.sim.generator import Profile
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID, C
from tests.helpers import baseline_versions, rv

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=500)


def test_good_rule_is_promoted_and_passes_canary() -> None:
    cand = rv(GOOD_RULE_ID, GOOD_RULE)
    shadow = evaluate_shadow(cand, baseline_versions(), CFG)
    assert shadow.outcome is Outcome.PROMOTE, shadow.reasons
    assert shadow.shadow is not None and shadow.shadow.ci_low > 0
    canary = evaluate_canary(cand, baseline_versions(), CFG)
    assert canary.outcome is Outcome.CANARY_PASSED, canary.reasons


def test_subtly_bad_rule_is_blocked_despite_better_average() -> None:
    v = evaluate_shadow(rv(BAD_RULE_ID, BAD_RULE), baseline_versions(), CFG)
    assert v.outcome is Outcome.BLOCK
    assert v.shadow is not None
    assert v.shadow.mean_improvement_usd_minor > 0  # the average looks good...
    failed = {c.name for c in v.checks if not c.passed}
    # ...but it breaks the late-evidence edge case and the escalation policy.
    assert {"no_new_policy_violations", "no_slice_regression", "golden_regression_suite"} <= failed
    overridden = {c.overridden_rule for c in v.shadow.conflicts}
    assert "base-late-evidence-accept@v1" in overridden


def test_rule_that_never_fires_is_held() -> None:
    never = RuleSpec(
        description="never",
        conditions=(C("mcc", "eq", "0000"),),
        action=Action.CONTEST,
        priority=60,
    )
    v = evaluate_shadow(rv("never-fires", never), baseline_versions(), CFG)
    assert v.outcome is Outcome.HOLD


def test_clearly_worse_rule_is_blocked() -> None:
    worse = RuleSpec(
        description="contest all nad",
        action=Action.CONTEST,
        priority=60,
        conditions=(C("reason_code", "eq", "not_as_described"),),
    )
    v = evaluate_shadow(rv("nad-contest", worse), baseline_versions(), CFG)
    assert v.outcome is Outcome.BLOCK
    assert any(c.name == "not_significantly_worse" and not c.passed for c in v.checks)


def test_canary_rolls_back_under_live_drift() -> None:
    # Live traffic shifts: delivery confirmations stop being trustworthy (e.g. a
    # fraud ring). The rule passed shadow on history but loses money live.
    drift = Profile(name="drift-delivery-fraud", p_valid_inr_delivered=0.75)
    cfg = CFG.model_copy(update={"canary_profile": drift})
    v = evaluate_canary(rv(GOOD_RULE_ID, GOOD_RULE), baseline_versions(), cfg)
    assert v.outcome is Outcome.ROLLBACK, v.reasons


def test_canary_holds_when_traffic_is_thin() -> None:
    cfg = CFG.model_copy(update={"canary_n_cases": 200, "canary_fraction": 0.02})
    v = evaluate_canary(rv(GOOD_RULE_ID, GOOD_RULE), baseline_versions(), cfg)
    assert v.outcome is Outcome.HOLD


def test_shadow_evaluation_is_deterministic() -> None:
    a = evaluate_shadow(rv(GOOD_RULE_ID, GOOD_RULE), baseline_versions(), CFG)
    b = evaluate_shadow(rv(GOOD_RULE_ID, GOOD_RULE), baseline_versions(), CFG)
    assert a.shadow == b.shadow and a.checks == b.checks


def test_bootstrap_is_seeded_and_brackets_the_mean() -> None:
    diffs = [0] * 900 + [100] * 80 + [-50] * 20
    a = paired_bootstrap_ci(diffs, 1000, 0.95, seed=1)
    assert a == paired_bootstrap_ci(diffs, 1000, 0.95, seed=1)
    mean, lo, hi = a
    assert lo < mean < hi and lo > 0


def test_bootstrap_degenerate_inputs() -> None:
    assert paired_bootstrap_ci([], 100, 0.95, 1) == (0.0, 0.0, 0.0)
    assert paired_bootstrap_ci([0, 0, 0], 100, 0.95, 1) == (0.0, 0.0, 0.0)


def test_bootstrap_interval_does_not_depend_on_case_order() -> None:
    """Regression (found by tests/test_metamorphic.py): resampling drew from the
    non-zero differences in arrival order, so the same evidence in another order gave
    another interval (here the upper bound moved from 0.18 to 0.27)."""
    diffs = [0] * 9 + [1, -2]
    assert paired_bootstrap_ci(diffs, 300, 0.95, 7) == paired_bootstrap_ci(
        list(reversed(diffs)), 300, 0.95, 7
    )
