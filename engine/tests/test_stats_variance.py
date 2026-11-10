"""Variance reduction (scruple.stats.variance): properties and exact checks (synthetic)."""

from __future__ import annotations

import math
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.stats import LossPair, StatsGateConfig, StratumSpec, evaluate_pairs
from scruple.stats.planning import stratum_cases_needed
from scruple.stats.sequential import BettingEProcess
from scruple.stats.variance import (
    ControlVariateEProcess,
    PostStratifiedEProcess,
    StratifiedBettingEProcess,
    VRGateConfig,
    crn_report,
    crn_violations,
    cv_cases_needed,
    evaluate_pairs_vr,
    mixture_radius,
    stratified_cases_needed,
)

unit = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False)
null_means = st.floats(min_value=-0.8, max_value=0.8)
sides = st.sampled_from(["greater", "less"])
labels = st.sampled_from(["a", "b", "c"])
stream = st.lists(st.tuples(labels, unit), min_size=1, max_size=60)


# --- stratified betting ---


@given(xs=st.lists(unit, min_size=1, max_size=80), m=null_means, side=sides)
def test_one_stratum_is_the_plain_e_process(xs: list[float], m: float, side: str) -> None:
    plain = BettingEProcess(m, side)  # type: ignore[arg-type]
    strat = StratifiedBettingEProcess(side, default_null_mean=m, activation=0.0)  # type: ignore[arg-type]
    for x in xs:
        plain.update(x)
        strat.update("only", x)
    assert strat.log_wealth == pytest.approx(plain.log_wealth, abs=1e-12)
    assert strat.max_log_wealth == pytest.approx(plain.max_log_wealth, abs=1e-12)


@given(data=stream, side=sides)
def test_stratified_wealth_positive_and_contributions_add_up(
    data: list[tuple[str, float]], side: str
) -> None:
    sp = StratifiedBettingEProcess(side, null_means={"a": 0.1, "b": -0.2})  # type: ignore[arg-type]
    for t, (k, x) in enumerate(data, start=1):
        lam = sp.next_bet(k)
        assert 0.0 <= lam <= sp.lambda_max(k) + 1e-15
        assert sp.next_bet(k) == lam  # computing the bet consumes nothing
        assert sp.update(k, x) > 0
        assert sp.log_wealth >= t * math.log(1 - sp.max_bet) - 1e-9
    assert math.fsum(sp.contributions.values()) == pytest.approx(sp.log_wealth, abs=1e-9)
    assert sum(sp.counts().values()) == sp.n == len(data)


@given(
    data=stream,
    k=labels,
    mean=st.floats(min_value=-0.9, max_value=0.0),
    spread=st.floats(min_value=0.0, max_value=1.0),
)
def test_stratified_factor_is_a_supermartingale_step(
    data: list[tuple[str, float]], k: str, mean: float, spread: float
) -> None:
    """For any history, a two-point law with mean <= m in the next case's type gives an
    expected factor of at most 1 (the step of Proposition 7.1)."""
    sp = StratifiedBettingEProcess("greater")
    for kk, x in data:
        sp.update(kk, x)
    lo, hi = max(-1.0, mean - spread), min(1.0, mean + spread)
    p_hi = 0.5 if hi == lo else (mean - lo) / (hi - lo)
    lam = sp.next_bet(k)
    expected = p_hi * (1 + lam * hi) + (1 - p_hi) * (1 + lam * lo)
    assert expected <= 1.0 + 1e-12


def test_stratified_sees_a_regression_the_pooled_test_cancels() -> None:
    """One type regresses, another improves by as much: pooled mean 0, stratified rejects."""
    rng = random.Random(1)
    pooled = BettingEProcess(0.0, "greater")
    strat = StratifiedBettingEProcess("greater")
    for _ in range(4000):
        k = "up" if rng.random() < 0.5 else "down"
        x = (0.2 if k == "up" else -0.2) + rng.uniform(-0.1, 0.1)
        pooled.update(x)
        strat.update(k, x)
    assert strat.rejected(0.01)
    assert not pooled.rejected(0.05)
    assert strat.contributions["up"] > 0 > strat.contributions["down"] - 1.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bet": 1.0},
        {"max_bet": 0.0},
        {"default_null_mean": 1.0},
        {"null_means": {"a": -1.0}},
        {"prior_second_moment": 0.0},
        {"activation": -1.0},
        {"alternative": "both"},
    ],
)
def test_stratified_rejects_bad_settings(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        StratifiedBettingEProcess(**kwargs)  # type: ignore[arg-type]


def test_stratified_rejection_is_sticky_and_checks_alpha() -> None:
    sp = StratifiedBettingEProcess("greater")
    for _ in range(300):
        sp.update("a", 0.6)
    assert sp.rejected(0.05)
    for _ in range(300):
        sp.update("a", -1.0)
    assert sp.rejected(0.05)  # the running maximum keeps it
    assert sp.max_e_value >= sp.e_value
    with pytest.raises(ValueError):
        sp.rejected(1.0)
    with pytest.raises(ValueError):
        sp.update("a", 1.5)


# --- control variates ---


@given(xs=st.lists(st.tuples(unit, unit), min_size=1, max_size=60), m=null_means, side=sides)
def test_zero_theta_is_the_plain_e_process(
    xs: list[tuple[float, float]], m: float, side: str
) -> None:
    plain = BettingEProcess(m, side)  # type: ignore[arg-type]
    cv = ControlVariateEProcess(m, side, 1, covariate_mean=[0.0], theta_max=0.0)  # type: ignore[arg-type]
    for x, z in xs:
        plain.update(x)
        cv.update(x, [z])
    assert cv.log_wealth == pytest.approx(plain.log_wealth, abs=1e-12)


@given(
    xs=st.lists(st.tuples(unit, unit, unit), min_size=1, max_size=50),
    m=null_means,
    side=sides,
    centre=st.sampled_from(["known", "radius", "running"]),
)
def test_control_variate_wealth_stays_positive(
    xs: list[tuple[float, float, float]], m: float, side: str, centre: str
) -> None:
    if centre == "running":
        cv = ControlVariateEProcess(m, side, 2, centre="running")  # type: ignore[arg-type]
    else:
        radius = [0.05, 0.5] if centre == "radius" else None
        cv = ControlVariateEProcess(
            m,
            side,  # type: ignore[arg-type]
            2,
            covariate_mean=[0.1, -0.2],
            covariate_radius=radius,
        )
    for t, (x, z1, z2) in enumerate(xs, start=1):
        assert cv.update(x, [z1, z2]) > 0
        assert cv.log_wealth >= t * math.log(1 - cv.max_bet) - 1e-9
        assert math.fsum(abs(v) for v in cv.coefficient()) <= cv.theta_max + 1e-12
    assert cv.exact == (centre == "known")


@given(
    hist=st.lists(st.tuples(unit, unit), min_size=0, max_size=40),
    zs=st.lists(unit, min_size=2, max_size=5),
    ws=st.lists(st.floats(min_value=0.01, max_value=1.0), min_size=5, max_size=5),
    target=st.floats(min_value=-0.4, max_value=0.0),
)
def test_known_mean_control_variate_step_has_mean_at_most_one(
    hist: list[tuple[float, float]], zs: list[float], ws: list[float], target: float
) -> None:
    """Exact expectation of the next factor under a joint law of (X, Z) with E[Z] = nu
    and E[X] <= 0, X correlated with Z: at most 1 (Proposition 7.3)."""
    probs = [w / sum(ws[: len(zs)]) for w in ws[: len(zs)]]
    nu = math.fsum(p * z for p, z in zip(probs, zs, strict=True))
    xs = [target + 0.3 * (z - nu) for z in zs]  # in [-1, 0.6], mean = target
    cv = ControlVariateEProcess(0.0, "greater", 1, covariate_mean=[nu])
    for x, z in hist:
        cv.update(x, [z])
    theta = cv.coefficient()[0]
    lam = cv._bet.bet(cv.lambda_max)
    expected = math.fsum(
        p * (1 + lam * (x - theta * (z - nu))) for p, x, z in zip(probs, xs, zs, strict=True)
    )
    assert expected <= 1.0 + 1e-12


def test_coefficient_recovers_a_linear_slope() -> None:
    rng = random.Random(3)
    cv = ControlVariateEProcess(0.0, "greater", 1, covariate_mean=[0.0], ridge=1e-6)
    for _ in range(3000):
        z = rng.uniform(-1, 1)
        cv.update(max(-1.0, min(1.0, 0.4 * z + rng.uniform(-0.2, 0.2))), [z])
    assert cv.coefficient()[0] == pytest.approx(0.4, abs=0.02)


def test_control_variate_beats_plain_when_the_covariate_explains_variance() -> None:
    rng = random.Random(4)
    plain = BettingEProcess(0.0, "greater")
    cv = ControlVariateEProcess(0.0, "greater", 1, covariate_mean=[0.0], theta_max=1.0)
    for _ in range(3000):
        z = rng.choice((-1.0, 1.0))
        x = 0.03 + 0.6 * z + rng.uniform(-0.05, 0.05)
        plain.update(max(-1.0, min(1.0, x)))
        cv.update(max(-1.0, min(1.0, x)), [z])
    assert cv.log_wealth > plain.log_wealth + 10


def test_running_centre_pays_a_penalty_that_shrinks() -> None:
    cv = ControlVariateEProcess(0.0, "greater", 1, centre="running", alpha_covariate=0.01)
    rng = random.Random(5)
    radii = []
    for _ in range(2000):
        z = rng.uniform(-1, 1)
        cv.update(max(-1.0, min(1.0, 0.5 * z)), [z])
        radii.append(cv.radius_now()[0])
    assert radii[-1] < radii[100] < 2.0
    assert cv.sum_penalty > 0
    assert not cv.exact


def test_mixture_radius_shape() -> None:
    assert mixture_radius(0, 0.05) == math.inf
    assert mixture_radius(10_000, 0.05) < mixture_radius(1_000, 0.05) < mixture_radius(100, 0.05)
    assert mixture_radius(1_000, 0.01) > mixture_radius(1_000, 0.1)


def test_mixture_radius_covers_by_simulation() -> None:
    """Time-uniform coverage of the running mean, uniform draws (synthetic, seeded)."""
    rng = random.Random(6)
    misses = 0
    for _ in range(300):
        s = 0.0
        for t in range(1, 1001):
            s += rng.uniform(-1, 1)
            if abs(s / t) >= mixture_radius(t, 0.1):
                misses += 1
                break
    assert misses / 300 <= 0.1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dim": 0, "covariate_mean": []},
        {"covariate_mean": None},
        {"covariate_mean": [2.0]},
        {"covariate_mean": [0.0], "covariate_radius": [-1.0]},
        {"covariate_mean": [0.0], "theta_max": -1.0},
        {"covariate_mean": [0.0], "ridge": 0.0},
        {"covariate_mean": [0.0], "max_bet": 1.0},
        {"centre": "running", "alpha_covariate": 0.0},
        {"centre": "other", "covariate_mean": [0.0]},
    ],
)
def test_control_variate_rejects_bad_settings(kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {"null_mean": 0.0, "alternative": "greater", "dim": 1} | kwargs
    with pytest.raises(ValueError):
        ControlVariateEProcess(**args)  # type: ignore[arg-type]


def test_control_variate_checks_dimensions() -> None:
    cv = ControlVariateEProcess(0.0, "greater", 2, covariate_mean=[0.0, 0.0])
    with pytest.raises(ValueError):
        cv.update(0.1, [0.0])
    with pytest.raises(ValueError):
        cv.update(0.1, [0.0, 3.0])


# --- post-stratification ---


@given(data=stream, m=null_means, side=sides)
def test_post_stratified_mean_is_preserved_and_wealth_positive(
    data: list[tuple[str, float]], m: float, side: str
) -> None:
    shares = {"a": 0.5, "b": 0.3, "c": 0.2}
    ps = PostStratifiedEProcess(shares, m, side)  # type: ignore[arg-type]
    for k, x in data:
        # E over the type draw of the adjustment -theta_k + offset is exactly 0
        adj = math.fsum(s * (-ps.theta(t) + ps.offset()) for t, s in shares.items())
        assert adj == pytest.approx(0.0, abs=1e-12)
        assert ps.update(k, x) > 0


def test_post_stratified_removes_between_type_variance() -> None:
    rng = random.Random(7)
    shares = {"a": 0.5, "b": 0.5}
    plain = BettingEProcess(0.0, "less")
    ps = PostStratifiedEProcess(shares, 0.0, "less")
    for _ in range(3000):
        k = "a" if rng.random() < 0.5 else "b"
        x = (0.4 if k == "a" else -0.4) - 0.02 + rng.uniform(-0.05, 0.05)
        plain.update(x)
        ps.update(k, x)
    assert ps.log_wealth > plain.log_wealth + 5


@pytest.mark.parametrize(
    "kwargs",
    [
        {"shares": {}},
        {"shares": {"a": 0.5}},
        {"shares": {"a": 1.0}, "null_mean": 1.0},
        {"shares": {"a": 1.0}, "theta_max": 2.0},
        {"shares": {"a": 1.0}, "max_bet": 0.0},
    ],
)
def test_post_stratified_rejects_bad_settings(kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {"null_mean": 0.0, "alternative": "less"} | kwargs
    with pytest.raises(ValueError):
        PostStratifiedEProcess(**args)  # type: ignore[arg-type]


def test_post_stratified_unknown_stratum() -> None:
    with pytest.raises(ValueError):
        PostStratifiedEProcess({"a": 1.0}, 0.0, "less").update("b", 0.0)


# --- common random numbers ---


def test_crn_report_identical_and_independent() -> None:
    rng = random.Random(8)
    base = [rng.uniform(0, 100) for _ in range(5000)]
    same = crn_report(base, base)
    assert same.var_paired == 0 and same.variance_ratio == 0 and same.share_identical == 1
    other = [rng.uniform(0, 100) for _ in range(5000)]
    indep = crn_report(base, other)
    assert indep.variance_ratio == pytest.approx(1.0, abs=0.06)
    assert abs(indep.correlation) < 0.05
    shifted = crn_report(base, [b + 1 for b in base])
    assert shifted.mean_diff == pytest.approx(1.0)
    assert shifted.correlation == pytest.approx(1.0)
    assert shifted.sample_size_ratio == shifted.variance_ratio


def test_crn_report_and_violations_validate() -> None:
    with pytest.raises(ValueError):
        crn_report([1.0], [1.0])
    with pytest.raises(ValueError):
        crn_report([1.0, 2.0], [1.0])
    assert crn_violations([0, 3, 0, -1], [False, True, False, False]) == [3]
    with pytest.raises(ValueError):
        crn_violations([0], [True, False])


# --- planning ---

SPEC = StratumSpec(
    name="s", frequency=0.1, cost_cap=10_000, min_detectable_regression=500, sd=2_000
)


def test_stratified_plan_matches_the_single_stratum_plan_at_full_alpha() -> None:
    assert stratified_cases_needed(SPEC, 0.05, 0.2) == stratum_cases_needed(SPEC, 0.05, 0.2)
    half = stratified_cases_needed(SPEC, 0.05, 0.2, share_of_alpha=0.5)[0]
    bonf = stratum_cases_needed(SPEC, 0.05 / 7, 0.2)[0]
    assert stratum_cases_needed(SPEC, 0.05, 0.2)[0] < half < bonf
    with pytest.raises(ValueError):
        stratified_cases_needed(SPEC, 0.05, 0.2, share_of_alpha=0.0)


def test_cv_plan_reduces_with_r2_and_matches_at_zero() -> None:
    zero = cv_cases_needed(SPEC, 0.05, 0.2, r2=0.0, theta_max=0.0)
    assert zero == stratum_cases_needed(SPEC, 0.05, 0.2)
    ns = [cv_cases_needed(SPEC, 0.05, 0.2, r2=r)[0] for r in (0.0, 0.3, 0.6, 0.9)]
    assert ns == sorted(ns, reverse=True)
    with pytest.raises(ValueError):
        cv_cases_needed(SPEC, 0.05, 0.2, r2=1.0)
    with pytest.raises(ValueError):
        cv_cases_needed(SPEC.model_copy(update={"sd": None}), 0.05, 0.2, r2=0.5)


# --- gate composition ---


def _pairs(means: dict[str, float], n: int, seed: int) -> list[LossPair]:
    rng = random.Random(seed)
    names = sorted(means)
    out = []
    for i in range(n):
        k = names[i % len(names)]
        d = means[k] + rng.uniform(-300, 300)
        out.append(
            LossPair(case_id=str(i), case_type=k, baseline_loss=1000.0, candidate_loss=1000.0 + d)
        )
    return out


def test_vr_verdict_blocks_a_clear_regression_and_holds_a_null() -> None:
    cfg = VRGateConfig(base=StatsGateConfig(cost_cap=1_000.0, shuffle_seed=0))
    bad = evaluate_pairs_vr(_pairs({"a": 150.0, "b": 0.0}, 2000, 1), cfg)
    assert bad.outcome == "block" and bad.stratified_rejected
    assert "stratified" in bad.blocked_by
    assert bad.contributions["a"] > bad.contributions["b"]
    null = evaluate_pairs_vr(_pairs({"a": -50.0, "b": -50.0}, 2000, 2), cfg)
    assert null.outcome != "block" and not null.blocked_by
    assert null.stratified_threshold == pytest.approx(1 / 0.04)


def test_vr_base_family_runs_at_its_share_of_alpha() -> None:
    pairs = _pairs({"a": 20.0, "b": 0.0}, 3000, 3)
    cfg = VRGateConfig(base=StatsGateConfig(cost_cap=1_000.0), stratified_share=0.25)
    v = evaluate_pairs_vr(pairs, cfg)
    direct = evaluate_pairs(pairs, StatsGateConfig(cost_cap=1_000.0, alpha_block=0.0375))
    assert v.base.regressed_fwer == direct.regressed_fwer


def test_vr_post_stratified_certificate() -> None:
    pairs = _pairs({"a": -120.0, "b": -120.0}, 4000, 4)
    cfg = VRGateConfig(base=StatsGateConfig(cost_cap=1_000.0), shares={"a": 0.5, "b": 0.5})
    v = evaluate_pairs_vr(pairs, cfg)
    assert v.certified and v.outcome == "promote"
    assert v.post_stratified_e_value is not None and v.post_stratified_e_value >= 20
    with pytest.raises(ValueError):
        evaluate_pairs_vr(pairs, cfg.model_copy(update={"shares": {"a": 1.0}}))
    with pytest.raises(ValueError):
        VRGateConfig(base=StatsGateConfig(cost_cap=1.0), shares={"a": 0.4})
    with pytest.raises(ValueError):
        VRGateConfig(base=StatsGateConfig(cost_cap=1.0), shares={"a": 0.0, "b": 1.0})
