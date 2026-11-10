"""Seeded Monte Carlo for the variance-reduced tests (synthetic): type-I error under
nulls with optional stopping, and power against the current test.

Every rate is a supremum over time (``ever rejected by n``), so it bounds the error of
every stopping rule, including data-dependent ones. Fast versions run by default; the
``slow`` versions use more paths and longer horizons (``make test-slow``).
"""

from __future__ import annotations

import math
import random

import pytest

from scruple.stats.sequential import BettingEProcess
from scruple.stats.simulate import (
    RareShock,
    Sampler,
    ScaledBeta,
    Ternary,
    binomial_se,
    ternary_with_mean,
)
from scruple.stats.variance import (
    ControlVariateEProcess,
    StratumLaw,
    block_rates,
    certificate_rates,
)

ALPHA = 0.05
SHARES = (0.28, 0.22, 0.20, 0.18, 0.06, 0.06)


def _strata(means: list[float], p: float = 0.2, mag: float = 0.5) -> list[StratumLaw]:
    return [
        StratumLaw(f"s{i}", s, ternary_with_mean(m, p, mag))
        for i, (s, m) in enumerate(zip(SHARES, means, strict=True))
    ]


def _shapes() -> list[StratumLaw]:
    """Different null shapes per type, every mean exactly 0 (the boundary)."""
    laws: list[Sampler] = [
        Ternary(0.05, 0.6, 0.05, 0.6),
        ScaledBeta(2.0, 2.0),
        RareShock(0.01, 0.0),
        Ternary(0.3, 0.1, 0.1, 0.3),
        ScaledBeta(0.5, 0.5),
        Ternary(0.0, 0.0, 0.0, 0.0),
    ]
    return [
        StratumLaw(f"s{i}", s, law) for i, (s, law) in enumerate(zip(SHARES, laws, strict=True))
    ]


NULLS = {
    "boundary": lambda: _strata([0.0] * 6),
    "mixed_negative": lambda: _strata([0.0, -0.02, -0.05, 0.0, -0.01, 0.0]),
    "shapes": _shapes,
}


def _bound(reps: int, level: float = ALPHA) -> float:
    return level + 3 * binomial_se(level, reps)


@pytest.mark.parametrize("name", sorted(NULLS))
def test_block_tests_hold_alpha_under_nulls(name: str) -> None:
    reps = 200
    rates = block_rates(NULLS[name](), 800, reps, seed=11, alpha=ALPHA)
    for method in ("current", "stratified", "vr"):
        assert rates[method] <= _bound(reps), (method, rates)


@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(NULLS))
def test_block_tests_hold_alpha_under_nulls_long(name: str) -> None:
    reps = 1500
    rates = block_rates(NULLS[name](), 3000, reps, seed=12, alpha=ALPHA, checkpoints=(500, 1500))
    for key, rate in rates.items():
        assert rate <= _bound(reps), (key, rates)


def test_stratified_is_more_powerful_than_the_current_test() -> None:
    """One type (6%) regresses, the others improve slightly: pooled cancels, Bonferroni
    pays K + 1; the stratified test and the composed rule see it more often."""
    rates = block_rates(_strata([-0.01] * 5 + [0.1]), 2000, 150, seed=13)
    assert rates["stratified"] >= rates["current"] + 0.15, rates
    assert rates["vr"] >= rates["current"] + 0.1, rates


def test_spread_regression_power_gain() -> None:
    rates = block_rates(_strata([0.015] * 6), 1500, 150, seed=14)
    assert rates["stratified"] >= rates["current"], rates


# --- certificates (marginal null) ---


def _hetero(offset: float) -> list[StratumLaw]:
    means = [0.3, -0.3, 0.2, -0.2, 0.1, -0.1]
    return [
        StratumLaw(f"s{i}", s, ternary_with_mean(m + offset, 0.6, 0.6))
        for i, (s, m) in enumerate(zip(SHARES, means, strict=True))
    ]


def _marginal(strata: list[StratumLaw]) -> float:
    return math.fsum(s.share * s.law.mean for s in strata)


def test_certificates_hold_alpha_at_the_marginal_boundary() -> None:
    """Strata means differ strongly; the marginal mean sits exactly on the null."""
    strata = _hetero(0.0)
    reps = 150
    rates = certificate_rates(strata, _marginal(strata), 600, reps, seed=15)
    for method, rate in rates.items():
        assert rate <= _bound(reps), (method, rates)


@pytest.mark.slow
def test_certificates_hold_alpha_at_the_marginal_boundary_long() -> None:
    strata = _hetero(0.0)
    reps = 1500
    rates = certificate_rates(strata, _marginal(strata), 3000, reps, seed=16)
    for method, rate in rates.items():
        assert rate <= _bound(reps), (method, rates)


def test_post_stratified_certificate_is_more_powerful() -> None:
    strata = _hetero(-0.03)
    rates = certificate_rates(strata, _marginal(strata) + 0.03, 1500, 60, seed=17)
    assert rates["post_stratified"] >= rates["plain"] + 0.1, rates
    assert rates["control_variate"] >= rates["plain"] + 0.05, rates


def test_running_centre_control_variate_holds_alpha_plus_covariate_level() -> None:
    rng = random.Random(18)
    reps, n, a_cov = 200, 800, 0.01
    hits = 0
    for _ in range(reps):
        cv = ControlVariateEProcess(0.0, "greater", 1, centre="running", alpha_covariate=a_cov)
        for _ in range(n):
            z = rng.uniform(-1, 1) * 0.8 + 0.1  # unknown mean 0.1
            cv.update(max(-1.0, min(1.0, 0.5 * (z - 0.1) + rng.uniform(-0.3, 0.3))), [z])
        hits += cv.rejected(ALPHA)
    assert hits / reps <= _bound(reps, ALPHA + a_cov)


def test_known_mean_cv_beats_plain_but_running_centre_does_not() -> None:
    """Section 9.4 heuristic: learning the centre from the stream costs more than the
    variance reduction gains near the detection boundary (synthetic, seeded)."""
    rng = random.Random(19)
    reps, n, mu = 60, 2000, 0.02
    wins = {"plain": 0, "known": 0, "running": 0}
    for _ in range(reps):
        plain = BettingEProcess(0.0, "greater")
        known = ControlVariateEProcess(0.0, "greater", 1, covariate_mean=[0.0])
        running = ControlVariateEProcess(0.0, "greater", 1, centre="running")
        for _ in range(n):
            z = rng.uniform(-1, 1)
            x = max(-1.0, min(1.0, mu + 0.4 * z + rng.uniform(-0.2, 0.2)))
            plain.update(x)
            known.update(x, [z])
            running.update(x, [z])
        wins["plain"] += plain.rejected(ALPHA)
        wins["known"] += known.rejected(ALPHA)
        wins["running"] += running.rejected(ALPHA)
    assert wins["known"] > wins["plain"] >= wins["running"], wins
