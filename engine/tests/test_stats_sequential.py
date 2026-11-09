"""Anytime-valid paired test: properties and seeded Monte Carlo checks (synthetic)."""

from __future__ import annotations

import itertools
import math
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.stats.sequential import (
    BettingConfidenceSequence,
    BettingEProcess,
    e_process_path,
    normalise_differences,
    sequential_test,
)
from scruple.stats.simulate import (
    RareShock,
    Sampler,
    ScaledBeta,
    Ternary,
    binomial_se,
    draw,
    peeking_z_rejects,
    stopped_rejections,
    stopping_rules,
    ternary_with_mean,
)

unit = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False)
xs_strategy = st.lists(unit, min_size=1, max_size=60)
null_means = st.floats(min_value=-0.9, max_value=0.9)
sides = st.sampled_from(["greater", "less"])
alphas = st.floats(min_value=0.001, max_value=0.5)

# --- properties ------------------------------------------------------------------------


@given(xs=xs_strategy, m=null_means, side=sides)
def test_e_value_positive_and_bounded_below(xs: list[float], m: float, side: str) -> None:
    ep = BettingEProcess(m, side)  # type: ignore[arg-type]
    for t, x in enumerate(xs, start=1):
        assert ep.update(x) > 0
        # each factor is at least 1 - max_bet
        assert ep.log_wealth >= t * math.log(1 - ep.max_bet) - 1e-9


@given(xs=xs_strategy, m=null_means, side=sides)
def test_bets_are_in_range_and_predictable(xs: list[float], m: float, side: str) -> None:
    ep = BettingEProcess(m, side)  # type: ignore[arg-type]
    for x in xs:
        lam = ep.next_bet()
        assert 0.0 <= lam <= ep.lambda_max + 1e-15
        before = ep.next_bet()
        assert before == lam  # computing the bet does not consume data
        ep.update(x)


@given(xs=xs_strategy, ys=xs_strategy, m=null_means, side=sides, frac=st.floats(0, 1))
def test_fixed_bet_e_values_multiply(
    xs: list[float], ys: list[float], m: float, side: str, frac: float
) -> None:
    lam = frac * 0.5 / (1 + (1 if side == "greater" else -1) * m)
    path = e_process_path(xs + ys, m, side, strategy="fixed", fixed_bet=lam)  # type: ignore[arg-type]
    a = e_process_path(xs, m, side, strategy="fixed", fixed_bet=lam)  # type: ignore[arg-type]
    b = e_process_path(ys, m, side, strategy="fixed", fixed_bet=lam)  # type: ignore[arg-type]
    assert path[-1] == pytest.approx(a[-1] + b[-1], abs=1e-9)
    sign = 1 if side == "greater" else -1
    direct = sum(math.log1p(lam * sign * (x - m)) for x in xs + ys)
    assert path[-1] == pytest.approx(direct, abs=1e-9)


@given(xs=xs_strategy, m=null_means)
def test_sides_are_mirror_images(xs: list[float], m: float) -> None:
    up = e_process_path(xs, m, "greater")
    down = e_process_path([-x for x in xs], -m, "less")
    assert up == pytest.approx(down, abs=1e-12)


@given(xs=xs_strategy, m=null_means, side=sides, a1=alphas, a2=alphas)
def test_rejection_is_monotone_in_alpha(
    xs: list[float], m: float, side: str, a1: float, a2: float
) -> None:
    ep = BettingEProcess(m, side)  # type: ignore[arg-type]
    ep.extend(xs)
    small, big = sorted((a1, a2))
    if ep.rejected(small):
        assert ep.rejected(big)


@given(xs=xs_strategy, m=null_means, side=sides)
def test_rejection_is_sticky(xs: list[float], m: float, side: str) -> None:
    ep = BettingEProcess(m, side)  # type: ignore[arg-type]
    seen = False
    for x in xs:
        ep.update(x)
        seen = seen or ep.log_wealth >= math.log(20)
        assert ep.rejected(0.05) == seen


@given(xs=xs_strategy, m=null_means)
def test_zero_bet_never_moves(xs: list[float], m: float) -> None:
    path = e_process_path(xs, m, "greater", strategy="fixed", fixed_bet=0.0)
    assert all(v == 0.0 for v in path)


@given(xs=xs_strategy)
def test_sequential_test_stops_at_first_crossing(xs: list[float]) -> None:
    res = sequential_test(xs, 0.0, "greater", 0.1)
    path = e_process_path(xs, 0.0, "greater")
    crossings = [i for i, v in enumerate(path, start=1) if v >= math.log(10)]
    if crossings:
        assert res.rejected and res.stopped_at == crossings[0]
    else:
        assert not res.rejected and res.stopped_at == len(xs)


@settings(max_examples=40)
@given(xs=st.lists(unit, min_size=1, max_size=40))
def test_confidence_sequence_is_an_interval_in_range(xs: list[float]) -> None:
    cs = BettingConfidenceSequence(0.05)
    cs.extend(xs)
    lo, hi = cs.interval()
    assert -1.0 <= lo <= hi <= 1.0


@settings(max_examples=30)
@given(xs=st.lists(unit, min_size=2, max_size=40), a1=alphas, a2=alphas)
def test_confidence_sequence_monotone_in_alpha(xs: list[float], a1: float, a2: float) -> None:
    small, big = sorted((a1, a2))
    wide, narrow = (
        BettingConfidenceSequence(small, bet_alpha=0.05),
        BettingConfidenceSequence(big, bet_alpha=0.05),
    )
    wide.extend(xs)
    narrow.extend(xs)
    wlo, whi = wide.interval()
    nlo, nhi = narrow.interval()
    assert wlo <= nlo + 1e-9 and nhi <= whi + 1e-9


@settings(max_examples=30)
@given(xs=st.lists(unit, min_size=2, max_size=60))
def test_nested_interval_shrinks(xs: list[float]) -> None:
    cs = BettingConfidenceSequence(0.1)
    prev = (-1.0, 1.0)
    for x in xs:
        cs.update(x)
        cur = cs.nested_interval()
        assert prev[0] <= cur[0] <= cur[1] <= prev[1]
        prev = cur


@given(x=unit)
def test_wealth_endpoint_rejects_exactly_outside_interval(x: float) -> None:
    """The CS endpoints agree with the wealth processes they invert."""
    cs = BettingConfidenceSequence(0.2, max_bet=0.45)
    rng = random.Random(int((x + 2) * 1000))
    cs.extend([x] * 5 + [rng.uniform(-1, 1) for _ in range(30)])
    lo, hi = cs.interval()
    thr = math.log(2 / 0.2)
    if lo > -1.0:
        assert cs._log_wealth(lo, 1) >= thr - 1e-9
        assert cs._log_wealth(min(lo + 1e-6, 1.0), 1) < thr + 1e-6
    if hi < 1.0:
        assert cs._log_wealth(hi, -1) >= thr - 1e-9


# --- input validation ---------------------------------------------------------------


@pytest.mark.parametrize("x", [1.5, -1.01, float("nan")])
def test_out_of_range_observation_rejected(x: float) -> None:
    with pytest.raises(ValueError, match="outside"):
        BettingEProcess(0.0, "greater").update(x)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"null_mean": 1.0, "alternative": "greater"},
        {"null_mean": 0.0, "alternative": "sideways"},
        {"null_mean": 0.0, "alternative": "greater", "max_bet": 1.0},
        {"null_mean": 0.0, "alternative": "greater", "strategy": "fixed"},
        {"null_mean": 0.0, "alternative": "greater", "strategy": "fixed", "fixed_bet": 0.9},
        {"null_mean": 0.0, "alternative": "greater", "strategy": "kelly"},
        {"null_mean": 0.0, "alternative": "greater", "prior_second_moment": 0.0},
    ],
)
def test_bad_parameters_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        BettingEProcess(**kwargs)  # type: ignore[arg-type]


def test_bad_alpha_and_cs_parameters() -> None:
    with pytest.raises(ValueError):
        BettingEProcess(0.0, "greater").rejected(0.0)
    with pytest.raises(ValueError):
        BettingConfidenceSequence(0.05, max_bet=0.5)
    with pytest.raises(ValueError):
        BettingConfidenceSequence(1.5)
    with pytest.raises(ValueError):
        BettingConfidenceSequence(0.05, bet_alpha=0.0)


def test_normalise_differences() -> None:
    assert normalise_differences([0, 100, 50], [100, 0, 50], 100) == [1.0, -1.0, 0.0]
    with pytest.raises(ValueError, match="exceeds cap"):
        normalise_differences([0], [101], 100)
    with pytest.raises(ValueError, match="positive"):
        normalise_differences([0], [1], 0)
    with pytest.raises(ValueError, match="same length"):
        normalise_differences([0, 1], [1], 10)


def test_huge_wealth_does_not_overflow() -> None:
    ep = BettingEProcess(-0.9, "greater")
    ep.extend([1.0] * 5000)
    assert math.isfinite(ep.e_value) and ep.rejected(1e-300)


def test_simulation_laws() -> None:
    t = ternary_with_mean(0.01, 0.1, 0.5)
    assert t.mean == pytest.approx(0.01)
    assert t.variance > 0
    assert RareShock(0.01, 0.0).mean == 0.0
    assert ScaledBeta(2, 2).mean == 0.0
    with pytest.raises(ValueError):
        ternary_with_mean(0.5, 0.1, 0.5)
    with pytest.raises(ValueError):
        Ternary(0.8, 1, 0.5, 1)
    with pytest.raises(ValueError):
        RareShock(0.5, -0.9)
    rng = random.Random(0)
    xs = draw(t, 20_000, rng)
    assert sum(xs) / len(xs) == pytest.approx(0.01, abs=0.005)


# --- Monte Carlo: type-I error under optional stopping --------------------------------

NULLS: dict[str, Sampler] = {
    "ternary_mostly_zero": ternary_with_mean(0.0, 0.1, 0.8),
    "continuous_beta": ScaledBeta(2.0, 2.0),
    "rare_large_loss": RareShock(0.02, 0.0),
    "always_changes": ternary_with_mean(0.0, 1.0, 1.0),
}


def _type_one(
    reps: int, horizon: int, seed: int, alternative: str = "greater"
) -> dict[str, dict[str, float]]:
    alpha = 0.05
    out: dict[str, dict[str, float]] = {}
    for k, (name, dist) in enumerate(NULLS.items()):
        rng = random.Random(seed + k)
        rules = stopping_rules(alpha, horizon, seed + k)
        counts: dict[str, int] = {}
        for _ in range(reps):
            xs = draw(dist, horizon, rng)
            if alternative == "less":
                xs = [-x for x in xs]
            res = stopped_rejections(xs, 0.0, alternative, alpha, rules)  # type: ignore[arg-type]
            for rule, rej in res.items():
                counts[rule] = counts.get(rule, 0) + rej
        out[name] = {rule: c / reps for rule, c in counts.items()}
    return out


def _assert_controlled(rates: dict[str, dict[str, float]], reps: int, alpha: float) -> None:
    slack = 3 * binomial_se(alpha, reps)
    for name, by_rule in rates.items():
        for rule, rate in by_rule.items():
            assert rate <= alpha + slack, (name, rule, rate)


def test_type_one_error_under_optional_stopping_fast() -> None:
    reps = 150
    rates = _type_one(reps=reps, horizon=300, seed=11)
    _assert_controlled(rates, reps, 0.05)


@pytest.mark.slow
@pytest.mark.parametrize("alternative", ["greater", "less"])
def test_type_one_error_under_optional_stopping_slow(alternative: str) -> None:
    reps = 2000
    rates = _type_one(reps=reps, horizon=2000, seed=101, alternative=alternative)
    _assert_controlled(rates, reps, 0.05)


def test_interior_null_rejects_less_than_boundary() -> None:
    """Under H0 with mu strictly inside the null, wealth shrinks: fewer rejections."""
    rng = random.Random(5)
    boundary = ternary_with_mean(0.0, 0.2, 0.8)
    inside = ternary_with_mean(-0.02, 0.2, 0.8)
    reps = 200
    b = sum(
        sequential_test(draw(boundary, 400, rng), 0.0, "greater", 0.1).rejected for _ in range(reps)
    )
    i = sum(
        sequential_test(draw(inside, 400, rng), 0.0, "greater", 0.1).rejected for _ in range(reps)
    )
    assert i <= b
    assert b / reps <= 0.1 + 3 * binomial_se(0.1, reps)


@pytest.mark.slow
def test_naive_peeking_inflates_type_one_but_betting_does_not() -> None:
    rng = random.Random(77)
    dist = ternary_with_mean(0.0, 0.1, 0.8)
    reps, horizon, alpha = 1000, 2000, 0.05
    naive = betting = 0
    for _ in range(reps):
        xs = draw(dist, horizon, rng)
        naive += peeking_z_rejects(xs, 0.0, alpha, every=10)
        betting += sequential_test(xs, 0.0, "greater", alpha).rejected
    assert naive / reps > 2 * alpha  # peeking a z-test is not valid
    assert betting / reps <= alpha + 3 * binomial_se(alpha, reps)


# --- Monte Carlo: power -----------------------------------------------------------------


def _power(mu: float, reps: int, horizon: int, seed: int) -> float:
    rng = random.Random(seed)
    dist = ternary_with_mean(mu, 0.1, 0.8)
    hits = sum(
        sequential_test(draw(dist, horizon, rng), 0.0, "greater", 0.05).rejected
        for _ in range(reps)
    )
    return hits / reps


def test_power_increases_with_effect_fast() -> None:
    p_small = _power(0.01, 60, 1500, 3)
    p_big = _power(0.04, 60, 1500, 3)
    assert p_big >= 0.9
    assert p_small <= p_big


@pytest.mark.slow
def test_power_curve_slow() -> None:
    powers = [_power(mu, 400, 3000, 9) for mu in (0.0, 0.005, 0.01, 0.02, 0.04)]
    assert powers[0] <= 0.05 + 3 * binomial_se(0.05, 400)
    assert all(a <= b + 0.03 for a, b in itertools.pairwise(powers))
    assert powers[3] >= 0.8 and powers[4] >= 0.99


# --- Monte Carlo: confidence-sequence coverage -----------------------------------------


def _coverage(reps: int, horizon: int, seed: int) -> float:
    rng = random.Random(seed)
    dist = ternary_with_mean(0.01, 0.2, 0.7)
    checkpoints = {25, 100, horizon // 2, horizon}
    covered = 0
    for _ in range(reps):
        cs = BettingConfidenceSequence(0.1)
        ok = True
        for t, x in enumerate(draw(dist, horizon, rng), start=1):
            cs.update(x)
            if t in checkpoints:
                lo, hi = cs.nested_interval()
                ok = ok and lo <= dist.mean <= hi
        covered += ok
    return covered / reps


def test_confidence_sequence_coverage_fast() -> None:
    reps = 60
    assert _coverage(reps, 300, 21) >= 0.9 - 3 * binomial_se(0.1, reps)


@pytest.mark.slow
def test_confidence_sequence_coverage_slow() -> None:
    reps = 500
    assert _coverage(reps, 2000, 23) >= 0.9 - 3 * binomial_se(0.1, reps)
