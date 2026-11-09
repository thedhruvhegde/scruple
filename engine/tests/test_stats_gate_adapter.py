"""The pure stats adapter: verdict logic, validation and error control (synthetic)."""

from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.stats import (
    LossModel,
    LossPair,
    NormalPrior,
    StatsGateConfig,
    evaluate_pairs,
)
from scruple.stats.gate_adapter import POOLED
from scruple.stats.simulate import binomial_se

TYPES = ("common_a", "common_b", "rare")
FREQS = (0.6, 0.39, 0.01)


def make_pairs(
    n: int, means: dict[str, float], seed: int, cap: float = 100.0, p_changed: float = 0.5
) -> list[LossPair]:
    """Losses in [0, cap]; candidate = baseline + diff with E[diff] = means[type]."""
    rng = random.Random(seed)
    out = []
    for i, t in enumerate(rng.choices(TYPES, weights=FREQS, k=n)):
        base = rng.uniform(0, cap / 2)
        mu = means.get(t, 0.0)
        # diff in {-cap/2, 0, +cap/2}; mean mu
        mag = cap / 2
        p_w = (p_changed + mu / mag) / 2
        u = rng.random()
        diff = mag if u < p_w else (-mag if u < p_changed else 0.0)
        out.append(
            LossPair(case_id=f"c{i}", case_type=t, baseline_loss=base, candidate_loss=base + diff)
        )
    return out


def cfg(**kw: object) -> StatsGateConfig:
    base: dict[str, object] = {"cost_cap": 100.0}
    base.update(kw)
    return StatsGateConfig.model_validate(base)


def test_clear_improvement_promotes() -> None:
    pairs = make_pairs(3000, {"common_a": -15.0, "common_b": -15.0, "rare": -15.0}, seed=1)
    v = evaluate_pairs(pairs, cfg())
    assert v.outcome == "promote" and v.certified
    assert v.ci_high < 0 and v.regressed_fwer == ()
    assert sum(t.n for t in v.types) == v.n_cases == 3000
    assert [t.case_type for t in v.types] == sorted(TYPES)


def test_rare_type_regression_blocks_even_when_average_improves() -> None:
    pairs = make_pairs(20_000, {"common_a": -2.0, "common_b": -2.0, "rare": 22.0}, seed=2)
    v = evaluate_pairs(pairs, cfg())
    assert v.mean_diff < 0  # the average improves
    assert v.outcome == "block"
    assert "rare" in v.regressed_fwer and POOLED not in v.regressed_fwer
    assert next(t for t in v.types if t.case_type == "rare").regressed


def test_pooled_regression_blocks() -> None:
    pairs = make_pairs(3000, dict.fromkeys(TYPES, 12.0), seed=3)
    v = evaluate_pairs(pairs, cfg())
    assert v.outcome == "block" and POOLED in v.regressed_fwer


def test_no_signal_holds() -> None:
    v = evaluate_pairs(make_pairs(500, {}, seed=4), cfg())
    assert v.outcome == "hold" and not v.certified
    assert any("not certified" in r for r in v.reasons)


def test_bayes_can_hold_a_certified_change() -> None:
    pairs = make_pairs(3000, dict.fromkeys(TYPES, -3.0), seed=5)
    plain = evaluate_pairs(pairs, cfg())
    if not plain.certified:
        pytest.skip("seed did not certify; covered by other tests")
    strict = evaluate_pairs(
        pairs,
        cfg(
            prior=NormalPrior(mean=20.0, sd=0.5),
            loss=LossModel(wrong_promote=1e6, wrong_block=1.0),
        ),
    )
    assert strict.decision is not None and not strict.decision.bayes_promote
    assert strict.outcome == "hold"
    assert any("Bayes rule blocks" in r for r in strict.reasons)


def test_bayes_agreeing_promotes_with_reason() -> None:
    pairs = make_pairs(3000, dict.fromkeys(TYPES, -15.0), seed=6)
    v = evaluate_pairs(pairs, cfg(prior=NormalPrior(mean=0.0, sd=20.0)))
    assert v.outcome == "promote" and v.decision is not None and v.decision.promote
    assert any("Bayes rule agrees" in r for r in v.reasons)


def test_deterministic_and_seeded_shuffle() -> None:
    pairs = make_pairs(800, {"rare": 30.0}, seed=7)
    assert evaluate_pairs(pairs, cfg()) == evaluate_pairs(pairs, cfg())
    a = evaluate_pairs(pairs, cfg(shuffle_seed=11))
    b = evaluate_pairs(list(reversed(pairs)), cfg(shuffle_seed=None))
    assert a == evaluate_pairs(pairs, cfg(shuffle_seed=11))
    assert a.mean_diff == pytest.approx(b.mean_diff)


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 10_000), shift=st.floats(-20, 20))
def test_verdict_invariants(seed: int, shift: float) -> None:
    v = evaluate_pairs(make_pairs(300, dict.fromkeys(TYPES, shift), seed=seed), cfg())
    assert (v.outcome == "block") == bool(v.regressed_fwer)
    if v.outcome == "promote":
        assert v.certified
    assert v.ci_low <= v.ci_high
    for t in v.types:
        assert t.ci_low <= t.ci_high and t.e_value >= 0 and t.max_e_value >= 1.0


def test_validation() -> None:
    with pytest.raises(ValueError, match="no loss pairs"):
        evaluate_pairs([], cfg())
    p = LossPair(case_id="x", case_type="a", baseline_loss=0, candidate_loss=1)
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_pairs([p, p], cfg())
    big = LossPair(case_id="y", case_type="a", baseline_loss=0, candidate_loss=101)
    with pytest.raises(ValueError, match="exceeds cap"):
        evaluate_pairs([big], cfg())
    with pytest.raises(ValueError, match="exceeds cap"):
        evaluate_pairs([p], cfg(type_caps={"a": 0.5}))
    with pytest.raises(ValidationError):
        cfg(type_caps={"a": 200.0})
    with pytest.raises(ValidationError):
        cfg(tolerance=100.0)
    with pytest.raises(ValidationError):
        cfg(type_tolerances={"a": 100.0})
    with pytest.raises(ValidationError):
        LossPair.model_validate(
            {"case_id": "x", "case_type": "a", "baseline_loss": 0, "candidate_loss": 0, "z": 1}
        )


def test_tolerances_and_weights_are_used() -> None:
    pairs = make_pairs(20_000, {"rare": 22.0}, seed=8)
    blocked = evaluate_pairs(pairs, cfg())
    tolerant = evaluate_pairs(pairs, cfg(type_tolerances={"rare": 40.0}))
    assert "rare" in blocked.regressed_fwer
    assert "rare" not in tolerant.regressed_fwer


# --- error control of the block family --------------------------------------------------


def _false_block_rate(reps: int, n: int, seed: int) -> float:
    blocks = 0
    for r in range(reps):
        v = evaluate_pairs(make_pairs(n, {}, seed=seed + r), cfg(alpha_block=0.1))
        blocks += v.outcome == "block"
    return blocks / reps


def test_false_block_rate_controlled_fast() -> None:
    reps = 40
    assert _false_block_rate(reps, 600, 100) <= 0.1 + 3 * binomial_se(0.1, reps)


@pytest.mark.slow
def test_false_block_rate_controlled_slow() -> None:
    reps = 400
    assert _false_block_rate(reps, 3000, 1000) <= 0.1 + 3 * binomial_se(0.1, reps)


@pytest.mark.slow
def test_false_promote_rate_controlled_slow() -> None:
    """At the certificate boundary (mean diff = tolerance) promotions are rare."""
    reps, alpha = 400, 0.1
    promotes = 0
    for r in range(reps):
        pairs = make_pairs(3000, {}, seed=5000 + r)
        promotes += evaluate_pairs(pairs, cfg(alpha_certify=alpha)).outcome == "promote"
    assert promotes / reps <= alpha + 3 * binomial_se(alpha, reps)
