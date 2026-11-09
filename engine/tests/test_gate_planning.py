"""Sample-size planning in the e-value gate: a held-out set too small for the
configured minimum detectable regression gives an explicit "insufficient
evidence" HOLD with the required n, never a silent pass. Synthetic data."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.gate.engine import Verdict, evaluate_shadow
from scruple.gate.evalue import mdr_for, plan_sample
from scruple.gate.models import Outcome
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.stats import StratumSpec, stratum_cases_needed
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

EV = GateConfig(stats="evalue", n_cases=3_000)
CAPS = {"a/<$80": 38_000.0, "b/<$80": 38_000.0, "c/<$10,000": 1_030_000.0}
TOLS = dict.fromkeys(CAPS, 0.0)


def _shadow(cfg: GateConfig, rule_id: str) -> Verdict:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), cfg)
    g.bootstrap_baseline()
    rv = g.propose(
        rule_id,
        GOOD_RULE if rule_id == GOOD_RULE_ID else BAD_RULE,
        owner="ops",
        provenance=PROV,
    )
    return evaluate_shadow(rv, g.registry.active_rules(), cfg)


def test_plan_matches_the_stats_planner_per_type() -> None:
    counts = {"a/<$80": 900, "b/<$80": 100, "c/<$10,000": 1_000}
    sp = plan_sample(counts, CAPS, TOLS, ["a/<$80", "b/<$80"], EV)
    assert [t.case_type for t in sp.types] == ["a/<$80", "b/<$80"]
    spec = StratumSpec(
        name="a",
        frequency=0.45,
        cost_cap=38_000.0,
        min_detectable_regression=0.025 * 38_000.0,
        sd=0.05 * 38_000.0,
    )
    expected, _ = stratum_cases_needed(spec, 0.05 / 2, 0.2)
    assert sp.types[0].required_n == expected
    assert sp.types[0].min_detectable_regression == pytest.approx(950.0)
    assert sp.types[0].planning_sd == pytest.approx(1_900.0)
    assert sp.insufficient == ("b/<$80",)  # 100 cases < required
    assert sp.required_total_cases > sum(counts.values())


def test_plan_uses_counts_and_config_only_and_is_deterministic() -> None:
    counts = {"a/<$80": 5_000, "b/<$80": 5_000, "c/<$10,000": 1}
    a = plan_sample(counts, CAPS, TOLS, ["a/<$80", "b/<$80"], EV)
    assert a == plan_sample(counts, CAPS, TOLS, ["a/<$80", "b/<$80"], EV)
    assert a.insufficient == ()


def test_more_tested_types_need_more_cases() -> None:
    counts = dict.fromkeys(CAPS, 2_000)
    one = plan_sample(counts, CAPS, TOLS, ["a/<$80"], EV)
    two = plan_sample(counts, CAPS, TOLS, ["a/<$80", "b/<$80"], EV)
    assert two.types[0].required_n > one.types[0].required_n  # alpha / K


@pytest.mark.parametrize(
    ("update", "direction"),
    [
        ({"evalue_min_detectable_regression_fraction": 0.05}, -1),
        ({"evalue_planning_sd_fraction": 0.2}, 1),
        ({"evalue_beta": 0.05}, 1),
        ({"evalue_alpha": 0.01}, 1),
    ],
)
def test_plan_moves_the_right_way(update: dict[str, float], direction: int) -> None:
    counts = dict.fromkeys(CAPS, 2_000)
    base = plan_sample(counts, CAPS, TOLS, ["a/<$80"], EV).types[0].required_n
    moved = plan_sample(counts, CAPS, TOLS, ["a/<$80"], EV.model_copy(update=update))
    assert (moved.types[0].required_n - base) * direction > 0


def test_mdr_overrides() -> None:
    cfg = EV.model_copy(
        update={
            "evalue_type_min_detectable_regression_usd_minor": {
                "late_evidence": 1_000.0,
                "late_evidence/<$80": 500.0,
            }
        }
    )
    assert mdr_for("late_evidence/<$80", 38_000.0, cfg) == 500.0
    assert mdr_for("late_evidence/<$10,000", 1_030_000.0, cfg) == 1_000.0
    assert mdr_for("fraud/<$80", 38_000.0, cfg) == pytest.approx(950.0)
    with pytest.raises(ValidationError):
        GateConfig(evalue_type_min_detectable_regression_usd_minor={"fraud": 0.0})
    with pytest.raises(ValidationError):
        GateConfig(evalue_beta=1.0)


def test_empty_family_needs_nothing() -> None:
    sp = plan_sample({"a/<$80": 10}, CAPS, TOLS, [], EV)
    assert sp.types == () and sp.required_total_cases == 0 and sp.insufficient == ()


def test_small_held_out_set_is_insufficient_evidence_not_a_pass() -> None:
    v = _shadow(EV, GOOD_RULE_ID)
    assert v.outcome is Outcome.HOLD
    assert v.summary.startswith("Insufficient evidence")
    check = next(c for c in v.checks if c.name == "evalue_sample_size")
    assert not check.passed and check.kind == "evidence"
    assert "needs" in check.detail and "3000 cases" in check.detail
    assert v.shadow is not None and v.shadow.evalue is not None
    ev = v.shadow.evalue
    assert not ev.sufficient and ev.insufficient
    assert ev.required_total_cases is not None and ev.required_total_cases > 3_000
    for t in ev.types:
        assert t.required_n is not None and t.min_detectable_regression is not None


def test_hard_failures_still_block_when_the_sample_is_small() -> None:
    v = _shadow(EV, BAD_RULE_ID)
    assert v.outcome is Outcome.BLOCK
    assert "evalue_sample_size" in {c.name for c in v.checks if not c.passed}


def test_planning_assumption_is_reported_not_decided() -> None:
    v = _shadow(EV.model_copy(update={"evalue_planning_sd_fraction": 0.001}), GOOD_RULE_ID)
    info = next(c for c in v.checks if c.name == "evalue_planning_assumption")
    assert info.kind == "info" and not info.passed
    assert v.shadow is not None and v.shadow.evalue is not None
    assert any(t.sd_within_assumption is False for t in v.shadow.evalue.types)


def test_sufficient_sample_lets_the_good_rule_through() -> None:
    """With a generous MDR the plan is small, and 6,000 cases suffice for the
    good rule's two tested types."""
    cfg = EV.model_copy(update={"n_cases": 6_000, "evalue_min_detectable_regression_fraction": 0.1})
    v = _shadow(cfg, GOOD_RULE_ID)
    check = next(c for c in v.checks if c.name == "evalue_sample_size")
    assert check.passed
    assert v.outcome in (Outcome.PROMOTE, Outcome.HOLD)
    assert not v.summary.startswith("Insufficient")
