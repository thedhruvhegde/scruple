"""Stratified e-value procedures: properties and seeded FWER/FDR Monte Carlo (synthetic)."""

from __future__ import annotations

import itertools
import random
from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.stats.sequential import BettingEProcess
from scruple.stats.simulate import Ternary, binomial_se, ternary_with_mean
from scruple.stats.stratified import (
    StratifiedMonitor,
    e_bh,
    e_bonferroni,
    e_closed_mean,
    normalise_weights,
)

e_lists = st.lists(st.floats(min_value=0, max_value=1e4, allow_nan=False), min_size=1, max_size=12)
alphas = st.floats(min_value=0.001, max_value=0.5)


def _brute_closed(e: Sequence[float], alpha: float) -> tuple[int, ...]:
    k = len(e)
    out = []
    for i in range(k):
        ok = True
        for r in range(1, k + 1):
            for s in itertools.combinations(range(k), r):
                if i in s and sum(e[j] for j in s) / r < 1 / alpha:
                    ok = False
        if ok:
            out.append(i)
    return tuple(out)


@given(e=st.lists(st.floats(0, 200), min_size=1, max_size=6), alpha=alphas)
def test_closed_mean_matches_brute_force(e: list[float], alpha: float) -> None:
    assert e_closed_mean(e, alpha) == _brute_closed(e, alpha)


@given(e=e_lists, alpha=alphas)
def test_closed_mean_dominates_bonferroni(e: list[float], alpha: float) -> None:
    assert set(e_bonferroni(e, alpha)) <= set(e_closed_mean(e, alpha))


@given(e=e_lists, alpha=alphas)
def test_ebh_dominates_bonferroni(e: list[float], alpha: float) -> None:
    assert set(e_bonferroni(e, alpha)) <= set(e_bh(e, alpha))


@given(e=e_lists, a1=alphas, a2=alphas)
def test_procedures_monotone_in_alpha(e: list[float], a1: float, a2: float) -> None:
    small, big = sorted((a1, a2))
    for proc in (e_bh, e_bonferroni, e_closed_mean):
        assert set(proc(e, small)) <= set(proc(e, big))


@given(e=e_lists, alpha=alphas, i=st.integers(0, 11), bump=st.floats(1, 100))
def test_procedures_monotone_in_e_values(e: list[float], alpha: float, i: int, bump: float) -> None:
    i %= len(e)
    bigger = list(e)
    bigger[i] *= bump
    for proc in (e_bh, e_bonferroni, e_closed_mean):
        assert set(proc(e, alpha)) <= set(proc(bigger, alpha))


@given(e=e_lists, alpha=alphas)
def test_ebh_rejection_set_is_top_r(e: list[float], alpha: float) -> None:
    rej = set(e_bh(e, alpha))
    if rej:
        assert min(e[i] for i in rej) >= max(
            (e[j] for j in range(len(e)) if j not in rej), default=0
        )
        assert min(e[i] for i in rej) >= len(e) / (alpha * len(rej)) - 1e-9


@given(e=e_lists, alpha=alphas)
def test_uniform_weights_equal_unweighted(e: list[float], alpha: float) -> None:
    w = [3.0] * len(e)
    assert e_bh(e, alpha, w) == e_bh(e, alpha)
    assert e_bonferroni(e, alpha, w) == e_bonferroni(e, alpha)


@given(w=st.lists(st.floats(0.01, 10), min_size=1, max_size=10))
def test_normalised_weights_sum_to_k(w: list[float]) -> None:
    assert sum(normalise_weights(w, len(w))) == pytest.approx(len(w))


def test_validation() -> None:
    with pytest.raises(ValueError):
        e_bh([-1.0], 0.1)
    with pytest.raises(ValueError):
        e_bh([1.0], 1.0)
    with pytest.raises(ValueError):
        normalise_weights([1.0], 2)
    with pytest.raises(ValueError):
        normalise_weights([0.0, 0.0], 2)
    with pytest.raises(ValueError):
        StratifiedMonitor(["a", "a"])
    with pytest.raises(ValueError, match="unknown stratum"):
        StratifiedMonitor(["a"]).update("b", 0.0)
    assert e_bh([], 0.1) == ()


def test_monitor_routes_cases_and_mixes_nulls() -> None:
    mon = StratifiedMonitor(["a", "b"], null_means={"a": 0.0, "b": 0.1}, weights={"a": 1, "b": 3})
    for _ in range(200):
        mon.update("a", 0.9)
    assert mon.counts() == {"a": 200, "b": 0}
    assert mon.e_values()["b"] == 1.0
    assert mon.fwer_rejections(0.05) == ("a",)
    assert mon.fdr_rejections(0.05) == ("a",)
    assert mon.closed_rejections(0.05) == ("a",)


def test_e_bh_known_example() -> None:
    # K=4, alpha=0.5: thresholds K/(alpha j) = 8, 4, 2.67, 2.
    assert e_bh([9.0, 4.5, 0.1, 2.1], 0.5) == (0, 1)
    assert e_bh([9.0, 4.5, 0.1, 2.7], 0.5) == (0, 1, 3)
    assert e_bonferroni([9.0, 4.5, 0.1, 2.1], 0.5) == (0,)


# --- Monte Carlo ------------------------------------------------------------------------

FREQS = (0.30, 0.20, 0.15, 0.12, 0.10, 0.08, 0.04, 0.01)


def _simulate(
    reps: int,
    n_cases: int,
    laws: Sequence[Ternary],
    freqs: Sequence[float],
    seed: int,
    *,
    comonotone: bool = False,
) -> dict[str, float]:
    """Error rates of the three procedures. ``laws[k].mean > 0`` means a real regression."""
    alpha = 0.1
    k = len(laws)
    nulls = {i for i in range(k) if laws[i].mean <= 0}
    rng = random.Random(seed)
    strata = [f"s{i}" for i in range(k)]
    fw = {"bonf_sup": 0, "closed": 0, "ebh": 0}
    fdp_sum = 0.0
    power_hits = 0
    for _ in range(reps):
        mon = StratifiedMonitor(strata)
        types = rng.choices(range(k), weights=freqs, k=n_cases)
        shared = [rng.random() for _ in range(n_cases)]  # comonotone: one u for all strata
        seen = [0] * k
        for t in types:
            u = shared[seen[t]] if comonotone else rng.random()
            seen[t] += 1
            mon.update(strata[t], _inverse_sample(laws[t], u))
        for name, rej in (
            ("bonf_sup", mon.fwer_rejections(alpha)),
            ("closed", mon.closed_rejections(alpha)),
        ):
            fw[name] += any(int(s[1:]) in nulls for s in rej)
        ebh = mon.fdr_rejections(alpha)
        false = sum(int(s[1:]) in nulls for s in ebh)
        fw["ebh"] += false > 0
        fdp_sum += false / max(len(ebh), 1)
        power_hits += any(int(s[1:]) not in nulls for s in mon.fwer_rejections(alpha))
    out = {f"fwer_{name}": v / reps for name, v in fw.items()}
    out["fdr_ebh"] = fdp_sum / reps
    out["any_true_detection"] = power_hits / reps
    return out


def _inverse_sample(law: Ternary, u: float) -> float:
    """Sample a ternary law from a uniform (so strata can share randomness)."""
    if u < law.p_worse:
        return law.worse
    if u < law.p_worse + law.p_better:
        return -law.better
    return 0.0


def _laws(regressed: Sequence[int], k: int) -> list[Ternary]:
    return [
        ternary_with_mean(0.2 if i in regressed else 0.0, 0.5, 0.8)
        if i in regressed
        else ternary_with_mean(0.0, 0.2, 0.8)
        for i in range(k)
    ]


@pytest.mark.parametrize("comonotone", [False, True])
def test_fwer_and_fdr_control_fast(comonotone: bool) -> None:
    reps = 80
    res = _simulate(reps, 1500, _laws([0, 7], 8), FREQS, seed=3, comonotone=comonotone)
    slack = 3 * binomial_se(0.1, reps)
    assert res["fwer_bonf_sup"] <= 0.1 + slack
    assert res["fwer_closed"] <= 0.1 + slack
    assert res["fdr_ebh"] <= 0.1 + slack


@pytest.mark.slow
@pytest.mark.parametrize("comonotone", [False, True])
@pytest.mark.parametrize("regressed", [(), (0,), (6, 7), (0, 1, 2, 3)])
def test_fwer_and_fdr_control_slow(comonotone: bool, regressed: tuple[int, ...]) -> None:
    reps = 600
    res = _simulate(reps, 4000, _laws(regressed, 8), FREQS, seed=31, comonotone=comonotone)
    slack = 3 * binomial_se(0.1, reps)
    k0 = 8 - len(regressed)
    assert res["fwer_bonf_sup"] <= 0.1 + slack
    assert res["fwer_closed"] <= 0.1 + slack
    assert res["fdr_ebh"] <= 0.1 * k0 / 8 + slack


def _pooled_vs_stratified(reps: int, n_cases: int, seed: int) -> tuple[float, float]:
    """Rare (1%) stratum regresses badly; common strata improve slightly."""
    alpha = 0.05
    rng = random.Random(seed)
    k = len(FREQS)
    laws = [ternary_with_mean(-0.01, 0.2, 0.5) for _ in range(k - 1)]
    laws.append(ternary_with_mean(0.35, 0.5, 0.8))
    pooled_mean = sum(f * law.mean for f, law in zip(FREQS, laws, strict=True))
    assert pooled_mean < 0  # the average improves
    strata = [f"s{i}" for i in range(k)]
    pooled_hits = strat_hits = 0
    for _ in range(reps):
        mon = StratifiedMonitor(strata)
        pooled = BettingEProcess(0.0, "greater")
        for t in rng.choices(range(k), weights=FREQS, k=n_cases):
            x = laws[t].sample(rng)
            mon.update(strata[t], x)
            pooled.update(x)
        pooled_hits += pooled.rejected(alpha)
        strat_hits += "s7" in mon.fwer_rejections(alpha)
    return pooled_hits / reps, strat_hits / reps


def test_pooled_misses_rare_regression_stratified_catches_fast() -> None:
    pooled, stratified = _pooled_vs_stratified(30, 8000, 5)
    assert pooled <= 0.05
    assert stratified >= 0.7


@pytest.mark.slow
def test_pooled_misses_rare_regression_stratified_catches_slow() -> None:
    pooled, stratified = _pooled_vs_stratified(300, 8000, 55)
    assert pooled <= 0.02
    assert stratified >= 0.8
