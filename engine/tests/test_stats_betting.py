"""Variance-aware bets (docs/math/07): validity, regret, the K_inf floor."""

from __future__ import annotations

import copy
import math
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.stats.betting import (
    BET_STRATEGIES,
    GridMixtureEProcess,
    PlugInEProcess,
    binary_kl,
    growth,
    make_e_process,
    oracle_growth,
    required_cases_lower_bound,
)
from scruple.stats.sequential import BettingEProcess

obs = st.lists(st.floats(min_value=-1.0, max_value=1.0), min_size=1, max_size=200)


@pytest.mark.parametrize("strategy", BET_STRATEGIES)
def test_type_one_error_at_the_null_boundary(strategy: str) -> None:
    reps, n, alpha = 400, 400, 0.05
    rng = random.Random(17)
    hits = 0
    for _ in range(reps):
        ep = make_e_process(strategy, 0.0, "greater")  # type: ignore[arg-type]
        for _ in range(n):
            # mean-zero, mostly zero, occasionally large: the shape of paired costs
            u = rng.random()
            ep.update(0.0 if u < 0.9 else (0.6 if u < 0.95 else -0.6))
        hits += ep.rejected(alpha)
    assert hits / reps <= alpha + 3 * math.sqrt(alpha * (1 - alpha) / reps)


@pytest.mark.parametrize("strategy", BET_STRATEGIES)
@pytest.mark.parametrize("alternative", ["greater", "less"])
def test_every_factor_is_a_fair_or_unfavourable_bet(strategy: str, alternative: str) -> None:
    """Exact check of E[K_t | past] <= K_{t-1} on a two-point null law with mean m."""
    m = 0.1
    ep = make_e_process(strategy, m, alternative)  # type: ignore[arg-type]
    rng = random.Random(5)
    for _ in range(50):
        before = ep.e_value
        # the two-point law {-1, 1} with mean m; average the next e-value over both outcomes
        up, down = copy.deepcopy(ep), copy.deepcopy(ep)
        e_up, e_down = up.update(1.0), down.update(-1.0)
        p_up = (1 + m) / 2
        assert p_up * e_up + (1 - p_up) * e_down <= before * (1 + 1e-9)
        ep.update(1.0 if rng.random() < p_up else -1.0)


@settings(max_examples=60, deadline=None)
@given(xs=obs, m=st.floats(-0.5, 0.5))
def test_mixture_regret_is_at_most_log_grid_size(xs: list[float], m: float) -> None:
    mix = GridMixtureEProcess(m, "greater")
    for x in xs:
        mix.update(x)
    _, best = mix.best_component()
    assert mix.log_wealth >= best - math.log(mix.size) - 1e-9
    assert mix.log_wealth <= best + 1e-9


@settings(max_examples=40, deadline=None)
@given(xs=obs)
def test_mixture_is_the_average_of_fixed_bets(xs: list[float]) -> None:
    mix = GridMixtureEProcess(0.0, "less", size=4)
    fixed = [BettingEProcess(0.0, "less", "fixed", 0.99, fixed_bet=b) for b in mix.bets]
    for x in xs:
        mix.update(x)
        for f in fixed:
            f.update(x)
    avg = sum(math.exp(f.log_wealth) for f in fixed) / len(fixed)
    assert math.exp(mix.log_wealth) == pytest.approx(avg, rel=1e-9)


@given(xs=obs)
def test_plugin_bets_are_predictable_and_bounded(xs: list[float]) -> None:
    ep = PlugInEProcess(0.2, "greater")
    top = 0.95 / 1.2
    for x in xs:
        lam = ep.next_bet()
        assert 0.0 <= lam <= top + 1e-12
        ep.update(x)


@settings(max_examples=60, deadline=None)
@given(xs=obs, m=st.floats(-0.5, 0.5), lam=st.floats(0.0, 0.99))
def test_oracle_growth_is_the_maximum(xs: list[float], m: float, lam: float) -> None:
    g, lam_star = oracle_growth(xs, m)
    assert 0.0 <= lam_star <= 1.0 / (1.0 + m)
    candidate = lam / (1.0 + m)
    assert growth(xs, m, candidate) <= g + 1e-9


@settings(max_examples=80, deadline=None)
@given(
    xs=st.lists(st.floats(-0.999, 1.0), min_size=5, max_size=60),
    shift=st.floats(0.01, 0.3),
    m=st.floats(-0.3, 0.3),
)
def test_k_inf_construction_attains_g_star(xs: list[float], shift: float, m: float) -> None:
    """Prop 7.2: Q = P / (1 + l* y), plus any missing mass at -1, is a null law at
    mean m with KL(P || Q) = g*, for interior and boundary maximisers alike."""
    xs = [max(-0.999, min(1.0, x + shift)) for x in xs]
    g, lam = oracle_growth(xs, m)
    if g <= 0.0:
        return
    n = len(xs)
    w = [1.0 / (1.0 + lam * (x - m)) for x in xs]
    mass = math.fsum(w) / n
    assert mass <= 1.0 + 1e-6
    extra = max(0.0, 1.0 - mass)  # put on x = -1
    if lam < 1.0 / (1.0 + m) * (1 - 1e-9):
        assert extra == pytest.approx(0.0, abs=1e-6)  # interior: Q needs no extra mass
    q_mean = math.fsum(wi * x for wi, x in zip(w, xs, strict=True)) / n + extra * -1.0
    assert q_mean == pytest.approx(m, abs=1e-6)  # Q is in the null (mean exactly m)
    kl = math.fsum(-math.log(wi) for wi in w) / n
    assert kl == pytest.approx(g, rel=1e-6, abs=1e-9)


def test_oracle_growth_edge_cases() -> None:
    assert oracle_growth([-0.1, -0.2], 0.0) == (0.0, 0.0)
    g, lam = oracle_growth([0.5, 0.5], 0.0)
    assert lam == 1.0 and g == pytest.approx(math.log(1.5))
    g2, lam2 = oracle_growth([-1.0, 0.9, 0.9, 0.9], 0.0)  # an atom at -1 keeps l* < 1
    assert 0 < lam2 < 1 and g2 > 0
    with pytest.raises(ValueError):
        oracle_growth([], 0.0)
    with pytest.raises(ValueError):
        oracle_growth([0.1], 1.0)
    assert growth([-1.0], 0.0, 1.0) == -math.inf


def test_sample_size_floor() -> None:
    assert binary_kl(0.8, 0.05) == pytest.approx(
        0.8 * math.log(0.8 / 0.05) + 0.2 * math.log(0.2 / 0.95)
    )
    assert required_cases_lower_bound(0.0, 0.05, 0.2) == math.inf
    a = required_cases_lower_bound(1e-3, 0.05, 0.2)
    b = required_cases_lower_bound(1e-3, 0.01, 0.2)
    assert 0 < a < b
    with pytest.raises(ValueError):
        required_cases_lower_bound(1e-3, 0.9, 0.2)


def test_mixture_beats_capped_agrapa_on_a_low_variance_regression() -> None:
    """Small but steady regression: the capped default bet leaves growth on the table."""
    rng = random.Random(2)
    xs = [0.01 + rng.uniform(-0.01, 0.01) for _ in range(3000)]
    out = {}
    for s in BET_STRATEGIES:
        ep = make_e_process(s, 0.0, "greater")
        for x in xs:
            ep.update(x)
        out[s] = math.log(max(ep.e_value, 1e-300))
    assert out["mixture"] > out["agrapa"]
    assert out["agrapa_wide"] > out["agrapa"]


def test_validation() -> None:
    with pytest.raises(ValueError):
        GridMixtureEProcess(0.0, "greater", ratio=1.5)
    with pytest.raises(ValueError):
        GridMixtureEProcess(0.0, "greater", size=0)
    with pytest.raises(ValueError):
        GridMixtureEProcess(1.0, "greater")
    with pytest.raises(ValueError):
        PlugInEProcess(0.0, "sideways")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PlugInEProcess(0.0, "greater", bet_alpha=0.0)
    with pytest.raises(ValueError):
        make_e_process("kelly", 0.0, "greater")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GridMixtureEProcess(0.0, "greater").update(2.0)
    with pytest.raises(ValueError):
        GridMixtureEProcess(0.0, "greater").rejected(1.0)
