"""EValueStatsProvider: the anytime-valid StatsProvider, and selection by GateConfig.stats."""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.gate.engine import evaluate_shadow
from scruple.gate.provider import (
    BootstrapProvider,
    EValueStatsProvider,
    StatsProvider,
    provider_for,
)
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

CAP = 1_000.0


def test_is_a_stats_provider() -> None:
    p = EValueStatsProvider(CAP)
    assert isinstance(p, StatsProvider)
    assert p.name == "betting-cs"


def test_config_selects_provider() -> None:
    assert isinstance(provider_for(GateConfig()), BootstrapProvider)
    p = provider_for(GateConfig(stats="evalue", evalue_cost_cap_usd_minor=123.0))
    assert isinstance(p, EValueStatsProvider)
    assert p.cost_cap == 123.0


def test_default_is_bootstrap() -> None:
    assert GateConfig().stats == "bootstrap"


def test_rejects_unknown_stats_and_bad_cap() -> None:
    with pytest.raises(ValidationError):
        GateConfig.model_validate({"stats": "magic"})
    with pytest.raises(ValidationError):
        GateConfig(evalue_cost_cap_usd_minor=0.0)
    with pytest.raises(ValueError):
        EValueStatsProvider(0.0)


def test_empty_inputs_give_zero_intervals() -> None:
    p = EValueStatsProvider(CAP)
    iv = p.paired_interval([], 0.95)
    assert (iv.mean, iv.low, iv.high, iv.n) == (0.0, 0.0, 0.0, 0)
    two = p.two_sample_interval([], [1.0], 0.9)
    assert (two.low, two.high, two.n) == (0.0, 0.0, 1)


def test_cap_violation_raises_instead_of_clipping() -> None:
    p = EValueStatsProvider(CAP)
    with pytest.raises(ValueError, match="cap"):
        p.paired_interval([10, int(CAP) + 1], 0.95)
    with pytest.raises(ValueError, match="cap"):
        p.two_sample_interval([CAP + 1], [0.0], 0.95)


@given(st.lists(st.integers(min_value=-1_000, max_value=1_000), min_size=1, max_size=200))
def test_paired_interval_contains_sample_mean_and_is_ordered(diffs: list[int]) -> None:
    iv = EValueStatsProvider(CAP).paired_interval(diffs, 0.95)
    assert -CAP <= iv.low <= iv.high <= CAP
    assert iv.n == len(diffs)
    assert iv.mean == pytest.approx(sum(diffs) / len(diffs))
    assert iv.method == "betting-cs-paired"


def test_paired_interval_is_deterministic_and_shrinks_with_n() -> None:
    rng = random.Random(4)
    diffs = [rng.randint(-100, 300) for _ in range(4_000)]
    p = EValueStatsProvider(CAP)
    a, b = p.paired_interval(diffs, 0.95), p.paired_interval(diffs, 0.95)
    assert a == b
    small = p.paired_interval(diffs[:200], 0.95)
    assert a.high - a.low < small.high - small.low
    assert a.low > 0  # true mean +100, cap 1,000: clearly positive at n = 4,000


def test_wider_than_bootstrap_on_the_same_data() -> None:
    """The price of anytime validity: never narrower than the fixed-n bootstrap here."""
    rng = random.Random(5)
    diffs = [rng.randint(-200, 200) for _ in range(1_000)]
    e = EValueStatsProvider(CAP).paired_interval(diffs, 0.95)
    b = BootstrapProvider(resamples=500, seed=1).paired_interval(diffs, 0.95)
    assert e.high - e.low > b.high - b.low


def test_two_sample_interval_detects_a_clear_difference() -> None:
    rng = random.Random(6)
    t = [rng.uniform(400, 1_000) for _ in range(1_500)]
    c = [rng.uniform(0, 600) for _ in range(1_500)]
    iv = EValueStatsProvider(CAP).two_sample_interval(t, c, 0.95)
    assert iv.low > 0
    assert iv.low <= iv.mean <= iv.high
    assert iv.method == "betting-cs-two-sample"


def test_paired_coverage_monte_carlo() -> None:
    """Seeded: the interval at the end of a stream misses the true mean rarely.
    Time-uniform coverage implies fixed-n coverage, so <= alpha up to MC error."""
    rng = random.Random(11)
    p = EValueStatsProvider(CAP)
    misses, reps = 0, 100
    for _ in range(reps):
        diffs = [rng.choice((-1_000, 0, 0, 0, 500)) for _ in range(300)]  # mean -100
        iv = p.paired_interval(diffs, 0.9)
        misses += not iv.low <= -100.0 <= iv.high
    assert misses / reps <= 0.1


def test_two_sample_coverage_monte_carlo() -> None:
    rng = random.Random(12)
    p = EValueStatsProvider(CAP)
    misses, reps = 0, 100
    for _ in range(reps):
        t = [rng.uniform(0, 1_000) for _ in range(150)]  # mean 500
        c = [rng.uniform(0, 600) for _ in range(150)]  # mean 300
        iv = p.two_sample_interval(t, c, 0.9)
        misses += not iv.low <= 200.0 <= iv.high
    assert misses / reps <= 0.1


def test_shadow_uses_the_evalue_interval_when_selected() -> None:
    cfg = GateConfig(n_cases=600, bootstrap_resamples=200, stats="evalue")
    gate = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), cfg)
    gate.bootstrap_baseline()
    rv = gate.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops", provenance=PROV)
    active = gate.registry.active_rules()
    v_e = evaluate_shadow(rv, active, cfg)
    v_b = evaluate_shadow(rv, active, cfg.model_copy(update={"stats": "bootstrap"}))
    assert v_e.shadow is not None and v_b.shadow is not None
    assert v_e.shadow.mean_improvement_usd_minor == v_b.shadow.mean_improvement_usd_minor
    width_e = v_e.shadow.ci_high - v_e.shadow.ci_low
    width_b = v_b.shadow.ci_high - v_b.shadow.ci_low
    assert width_e > width_b


def test_service_shadow_recorder_follows_config() -> None:
    gate = AgentGate(
        SQLiteStorage(":memory:"), SteppingClock(), GateConfig(n_cases=500, stats="evalue")
    )
    gate.bootstrap_baseline()
    gate.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops", provenance=PROV)
    rec = gate.shadow_recorder(GOOD_RULE_ID, 1)
    assert isinstance(rec._provider, EValueStatsProvider)
