"""063: counterfactual cost delta against the baseline policy on the same case."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.agents.dispute_baselines import (
    always_accept,
    baseline_result,
    evidence_policy,
    run_policy,
)
from scruple.agents.env import FAMILIES
from scruple.verifiers.counterfactual import COUNTERFACTUAL, cost_delta
from scruple.verifiers.record import BaselineResult

_REC = run_policy(always_accept, 11).record()


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", range(5))
def test_the_baseline_itself_scores_zero_delta(seed: int, family: str) -> None:
    rec = run_policy(evidence_policy, seed, family).record()
    assert rec.baseline is not None and rec.baseline.policy == "evidence"
    assert cost_delta(rec) == 0
    res = COUNTERFACTUAL.run(rec)
    assert res.passed and res.score == 1.0 and not res.hard


@pytest.mark.parametrize("seed", range(8))
def test_baseline_replay_is_deterministic_and_matches_the_same_case(seed: int) -> None:
    a = baseline_result(seed, None)
    baseline_result.cache_clear()
    assert baseline_result(seed, None) == a
    rec = run_policy(always_accept, seed).record()
    assert rec.baseline == a
    assert cost_delta(rec) == rec.cost_usd_minor - a.cost_usd_minor


def test_accepting_everything_is_worse_than_baseline_in_aggregate() -> None:
    deltas = [cost_delta(run_policy(always_accept, s).record()) or 0 for s in range(40)]
    assert sum(deltas) > 0
    assert sum(d > 0 for d in deltas) > sum(d < 0 for d in deltas)


def test_missing_baseline_is_neutral() -> None:
    rec = _REC.model_copy(update={"baseline": None})
    res = COUNTERFACTUAL.run(rec)
    assert res.passed and "no baseline" in res.explanation


@settings(max_examples=60, deadline=None)
@given(st.integers(-50_000, 50_000), st.integers(0, 40_000))
def test_score_is_monotone_in_the_delta(base: int, extra: int) -> None:
    def score(cost: int) -> float:
        b = BaselineResult(policy="evidence", cost_usd_minor=base, resolution="won", correct=True)
        rec = _REC.model_copy(update={"baseline": b, "cost_usd_minor": cost})
        return COUNTERFACTUAL.run(rec).score

    lo, hi = score(base + extra), score(base + extra + 500)
    assert 0.0 <= hi <= lo <= 1.0
    assert score(base - 1) == 1.0
