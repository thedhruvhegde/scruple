"""Sample-size planning: lemmas, exact binomial counts, and simulation validation (synthetic)."""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.stats.planning import (
    StratumSpec,
    binomial_sf,
    growth_lower_bound,
    guaranteed_count,
    max_log_threshold,
    plan,
    simulate_power,
    stratum_cases_needed,
    total_cases_for_count,
)
from scruple.stats.simulate import Ternary, ternary_with_moments


def spec(
    freq: float = 0.05, mde: float = 500, sd: float | None = 2000, tol: float = 0.0
) -> StratumSpec:
    return StratumSpec(
        name="s",
        frequency=freq,
        cost_cap=10_000,
        min_detectable_regression=mde,
        sd=sd,
        tolerance=tol,
    )


# --- Lemma 1 and the growth bound ---------------------------------------------------------


@given(a=st.floats(0, 0.99), u=st.floats(0, 1), big=st.floats(0, 50))
def test_log_lower_bound_lemma(a: float, u: float, big: float) -> None:
    for x in (-a * u, big):
        assert math.log1p(x) >= x - x * x / (2 * (1 - a)) - 1e-12


@given(
    p_w=st.floats(0.0, 0.5),
    p_b=st.floats(0.0, 0.5),
    w=st.floats(0.0, 1.0),
    b=st.floats(0.0, 1.0),
    m=st.floats(-0.5, 0.5),
    frac=st.floats(0.0, 0.99),
)
def test_growth_bound_below_exact_growth(
    p_w: float, p_b: float, w: float, b: float, m: float, frac: float
) -> None:
    law = Ternary(p_w, w, p_b, b)
    lam = frac / (1 + m)
    exact = (
        p_w * math.log1p(lam * (w - m))
        + p_b * math.log1p(lam * (-b - m))
        + (1 - p_w - p_b) * math.log1p(-lam * m)
    )
    bound = growth_lower_bound(lam, law.mean - m, math.sqrt(max(law.variance, 0.0)), m)
    assert bound <= exact + 1e-9


def test_growth_bound_rejects_oversized_bet() -> None:
    with pytest.raises(ValueError):
        growth_lower_bound(1.0, 0.1, 0.1, 0.0)


# --- monotonicity of the plan -----------------------------------------------------------


@pytest.mark.parametrize("method", ["bernstein", "hoeffding", "clt"])
def test_cases_needed_monotone(method: str) -> None:
    base = stratum_cases_needed(spec(), 0.01, 0.2, method)[0]  # type: ignore[arg-type]
    assert stratum_cases_needed(spec(), 0.001, 0.2, method)[0] >= base  # type: ignore[arg-type]
    assert stratum_cases_needed(spec(), 0.01, 0.05, method)[0] >= base  # type: ignore[arg-type]
    assert stratum_cases_needed(spec(mde=1000), 0.01, 0.2, method)[0] <= base  # type: ignore[arg-type]
    assert stratum_cases_needed(spec(sd=4000), 0.01, 0.2, method)[0] >= base  # type: ignore[arg-type]


def test_method_ordering_and_minimality() -> None:
    n_b = stratum_cases_needed(spec(), 0.01, 0.2, "bernstein")[0]
    n_h = stratum_cases_needed(spec(), 0.01, 0.2, "hoeffding")[0]
    n_c = stratum_cases_needed(spec(), 0.01, 0.2, "clt")[0]
    assert n_c <= n_b <= n_h
    target = math.log(100)
    assert max_log_threshold(spec(), n_b, 0.2) >= target
    assert max_log_threshold(spec(), n_b - 1, 0.2) < target
    assert max_log_threshold(spec(), 0, 0.2) == -math.inf


def test_worst_case_variance_used_when_sd_missing() -> None:
    assert spec(sd=None).sigma == pytest.approx(math.sqrt(1 - 0.05**2))
    assert spec(sd=10**9).sigma == spec(sd=None).sigma
    assert (
        stratum_cases_needed(spec(sd=None), 0.01, 0.2)[0]
        > stratum_cases_needed(spec(), 0.01, 0.2)[0]
    )


def test_spec_validation() -> None:
    with pytest.raises(ValidationError):
        spec(mde=10_000)
    with pytest.raises(ValidationError):
        StratumSpec(name="s", frequency=0.0, cost_cap=1, min_detectable_regression=0.5)
    with pytest.raises(ValidationError):
        StratumSpec.model_validate(
            {"name": "s", "frequency": 0.1, "cost_cap": 1, "min_detectable_regression": 0.1, "x": 1}
        )
    with pytest.raises(ValueError):
        stratum_cases_needed(spec(), 0.0, 0.2)
    with pytest.raises(ValueError):
        plan([])
    with pytest.raises(ValueError):
        plan([spec(), spec()])
    with pytest.raises(ValueError):
        plan([spec()], daily_cases=0)


# --- exact binomial counts --------------------------------------------------------------


def _brute_sf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, j) * p**j * (1 - p) ** (n - j) for j in range(max(k, 0), n + 1))


@given(n=st.integers(0, 120), k=st.integers(-2, 125), p=st.floats(0.0, 1.0))
def test_binomial_sf_exact(n: int, k: int, p: float) -> None:
    assert binomial_sf(k, n, p) == pytest.approx(min(_brute_sf(k, n, p), 1.0), abs=1e-9)


@settings(max_examples=40)
@given(needed=st.integers(1, 300), p=st.floats(0.005, 1.0), gamma=st.floats(0.01, 0.5))
def test_total_cases_is_minimal(needed: int, p: float, gamma: float) -> None:
    n_tot = total_cases_for_count(needed, p, gamma)
    assert binomial_sf(needed, n_tot, p) >= 1 - gamma
    assert binomial_sf(needed, n_tot - 1, p) < 1 - gamma
    assert guaranteed_count(n_tot, p, gamma) >= needed
    assert guaranteed_count(n_tot - 1, p, gamma) < needed


def test_total_cases_edge_cases() -> None:
    assert total_cases_for_count(0, 0.1, 0.05) == 0
    assert total_cases_for_count(5, 1.0, 0.05) == 5
    with pytest.raises(ValueError):
        total_cases_for_count(5, 0.0, 0.05)


# --- full plans ---------------------------------------------------------------------------

FREQS = (0.30, 0.20, 0.15, 0.12, 0.10, 0.08, 0.04, 0.01)


def _specs() -> list[StratumSpec]:
    return [
        StratumSpec(
            name=f"s{i}", frequency=f, cost_cap=10_000, min_detectable_regression=500, sd=2000
        )
        for i, f in enumerate(FREQS)
    ]


def test_rare_stratum_binds_and_days_scale() -> None:
    p = plan(_specs(), daily_cases=2000, canary_fraction=0.1)
    assert p.binding_stratum == "s7"
    assert p.days == pytest.approx(p.total_cases / 200)
    assert all(s.alpha == pytest.approx(0.05 / 8) for s in p.strata)


def test_optimised_allocation_beats_uniform_and_spends_alpha() -> None:
    uni = plan(_specs())
    opt = plan(_specs(), allocation="optimised")
    assert opt.total_cases <= uni.total_cases
    assert sum(s.alpha for s in opt.strata) == pytest.approx(0.05)
    rare = next(s for s in opt.strata if s.name == "s7")
    common = next(s for s in opt.strata if s.name == "s0")
    assert rare.alpha > common.alpha
    assert all(s.total_cases <= opt.total_cases for s in opt.strata)


# --- simulation validation --------------------------------------------------------------


def test_moment_matched_law() -> None:
    law = ternary_with_moments(0.05, 0.2)
    assert law.mean == pytest.approx(0.05)
    assert math.sqrt(law.variance) == pytest.approx(0.2)
    with pytest.raises(ValueError):
        ternary_with_moments(0.0, 1.5, magnitude=1.0)


def test_planned_n_achieves_power_fast() -> None:
    s = spec(mde=1000, sd=2500)
    n, bet = stratum_cases_needed(s, 0.01, 0.2)
    assert simulate_power(s, n, 0.01, bet=bet, reps=150, seed=1) >= 0.8


@pytest.mark.slow
@pytest.mark.parametrize(
    ("mde", "sd", "tol"),
    [(500, 2000, 0.0), (1000, 2500, 200.0), (300, 1000, 0.0), (2000, None, 0.0)],
)
@pytest.mark.parametrize("method", ["bernstein", "hoeffding"])
def test_planned_n_achieves_power_slow(
    mde: float, sd: float | None, tol: float, method: str
) -> None:
    s = spec(mde=mde, sd=sd, tol=tol)
    n, bet = stratum_cases_needed(s, 0.005, 0.2, method)  # type: ignore[arg-type]
    assert simulate_power(s, n, 0.005, bet=bet, reps=600, seed=7) >= 0.8 - 3 * math.sqrt(0.16 / 600)
