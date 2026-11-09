"""Asymmetric decision rule and its composition with the e-value certificate (synthetic)."""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.stats.decision import (
    LossModel,
    NormalPrior,
    Posterior,
    alpha_for_budget,
    bayes_promote,
    bayes_threshold,
    composed_decision,
    mean_and_se,
    normal_posterior,
    posterior_risks,
    realised_loss,
    simulate_rules,
)

costs = st.floats(min_value=0.01, max_value=1000)
means = st.floats(min_value=-5, max_value=5)
sds = st.floats(min_value=0.01, max_value=5)


@given(c_a=costs, c_b=costs, mu=means, s=sds, tol=st.floats(-1, 1))
def test_zero_one_bayes_rule_is_posterior_threshold(
    c_a: float, c_b: float, mu: float, s: float, tol: float
) -> None:
    loss = LossModel(wrong_promote=c_a, wrong_block=c_b)
    post = Posterior(mean=mu, sd=s)
    p_harm = 1 - NormalDist(mu, s).cdf(tol)
    p_star = bayes_threshold(loss)
    if abs(p_harm - p_star) > 1e-9:
        assert bayes_promote(post, tol, loss) == (p_harm <= p_star)


def _numeric_expectations(mu: float, s: float, tol: float) -> tuple[float, float]:
    d = NormalDist(mu, s)
    grid = 4001
    lo, hi = mu - 10 * s, mu + 10 * s
    h = (hi - lo) / (grid - 1)
    excess = saving = 0.0
    for i in range(grid):
        x = lo + i * h
        w = (0.5 if i in (0, grid - 1) else 1.0) * h * d.pdf(x)
        excess += w * max(x - tol, 0.0)
        saving += w * max(tol - x, 0.0)
    return excess, saving


@given(mu=st.floats(-2, 2), s=st.floats(0.05, 2), tol=st.floats(-1, 1))
def test_linear_closed_forms_match_quadrature(mu: float, s: float, tol: float) -> None:
    loss = LossModel(kind="linear", wrong_promote=1.0, wrong_block=1.0)
    rho_p, rho_b = posterior_risks(Posterior(mean=mu, sd=s), tol, loss)
    ex, sv = _numeric_expectations(mu, s, tol)
    assert rho_p == pytest.approx(ex, rel=1e-4, abs=1e-5)
    assert rho_b == pytest.approx(sv, rel=1e-4, abs=1e-5)
    assert rho_p - rho_b == pytest.approx(mu - tol, abs=1e-9)  # E(x)^+ - E(-x)^+ = E x


@given(c_a=costs, c_b=costs, mu=means, s=sds, e=st.floats(0, 1e6), alpha=st.floats(0.001, 0.5))
def test_composed_promotes_only_with_both(
    c_a: float, c_b: float, mu: float, s: float, e: float, alpha: float
) -> None:
    d = composed_decision(
        e, alpha, Posterior(mean=mu, sd=s), 0.0, LossModel(wrong_promote=c_a, wrong_block=c_b)
    )
    assert d.promote == (d.certified and d.bayes_promote)
    assert d.certified == (e >= 1 / alpha)
    assert 0.0 <= d.prob_harm <= 1.0


@given(c_a=costs, c_b=costs, bump=st.floats(1, 100), mu=means, s=sds, e=st.floats(0, 1e4))
def test_monotone_in_costs_and_evidence(
    c_a: float, c_b: float, bump: float, mu: float, s: float, e: float
) -> None:
    post = Posterior(mean=mu, sd=s)
    base = composed_decision(e, 0.05, post, 0.0, LossModel(wrong_promote=c_a, wrong_block=c_b))
    pricier = composed_decision(
        e, 0.05, post, 0.0, LossModel(wrong_promote=c_a * bump, wrong_block=c_b)
    )
    more_evidence = composed_decision(
        e * bump, 0.05, post, 0.0, LossModel(wrong_promote=c_a, wrong_block=c_b)
    )
    if pricier.promote:
        assert base.promote
    if base.promote:
        assert more_evidence.promote


@given(m0=means, tau=sds, est=means, se=sds)
def test_posterior_is_precision_weighted(m0: float, tau: float, est: float, se: float) -> None:
    post = normal_posterior(NormalPrior(mean=m0, sd=tau), est, se)
    assert min(m0, est) - 1e-9 <= post.mean <= max(m0, est) + 1e-9
    assert post.sd <= min(tau, se) + 1e-12


def test_posterior_limits_and_validation() -> None:
    post = normal_posterior(NormalPrior(mean=5.0, sd=1.0), 0.0, 1e-6)
    assert post.mean == pytest.approx(0.0, abs=1e-6)
    with pytest.raises(ValueError):
        normal_posterior(NormalPrior(sd=1.0), 0.0, 0.0)
    with pytest.raises(ValidationError):
        NormalPrior(sd=0.0)
    with pytest.raises(ValidationError):
        LossModel(wrong_promote=0.0, wrong_block=1.0)
    with pytest.raises(ValueError):
        composed_decision(1.0, 1.5, post, 0.0, LossModel(wrong_promote=1, wrong_block=1))
    with pytest.raises(ValueError):
        composed_decision(-1.0, 0.05, post, 0.0, LossModel(wrong_promote=1, wrong_block=1))
    with pytest.raises(ValueError):
        mean_and_se([1.0])
    assert mean_and_se([1.0, 1.0])[1] > 0


def test_alpha_for_budget() -> None:
    loss = LossModel(wrong_promote=1_000_000, wrong_block=10_000)
    assert alpha_for_budget(loss, 50_000) == pytest.approx(0.05)
    with pytest.raises(ValueError):
        alpha_for_budget(loss, 2_000_000)
    with pytest.raises(ValueError):
        alpha_for_budget(LossModel(kind="linear", wrong_promote=1, wrong_block=1), 0.5)


@pytest.mark.parametrize("kind", ["zero_one", "linear"])
def test_realised_loss(kind: str) -> None:
    loss = LossModel(kind=kind, wrong_promote=10, wrong_block=1, volume=2)  # type: ignore[arg-type]
    assert realised_loss(True, -0.1, 0.0, loss) == 0.0
    assert realised_loss(False, 0.1, 0.0, loss) == 0.0
    assert realised_loss(True, 0.1, 0.0, loss) == pytest.approx(10 if kind == "zero_one" else 2.0)
    assert realised_loss(False, -0.1, 0.0, loss) == pytest.approx(1 if kind == "zero_one" else 0.2)


# --- Monte Carlo --------------------------------------------------------------------------


def _check(stats_alpha: float, reps: int, c_a: float, c_b: float, n: int, seed: int) -> None:
    res = simulate_rules(
        NormalPrior(mean=-0.01, sd=0.03),
        0.0,
        LossModel(wrong_promote=c_a, wrong_block=c_b),
        alpha=stats_alpha,
        n=n,
        reps=reps,
        seed=seed,
    )
    slack = 3 * math.sqrt(stats_alpha * (1 - stats_alpha) / reps)
    assert res.boundary_promote_rate["composed"] <= stats_alpha + slack
    assert res.boundary_promote_rate["frequentist"] <= stats_alpha + slack
    # composed is Bayes-optimal among certified rules: no worse than certificate alone
    se = res.risk_diff_se
    post = res.posterior_risk  # Proposition 2(ii) holds exactly for the model's own risk
    assert post["bayes"] <= post["composed"] + 1e-12
    assert post["composed"] <= post["frequentist"] + 1e-12
    assert post["composed"] <= post["always_block"] + 1e-12
    assert (
        res.bayes_risk["composed"] <= res.bayes_risk["frequentist"] + 3 * se["frequentist"] + 1e-12
    )
    assert res.bayes_risk["bayes"] <= res.bayes_risk["composed"] + 3 * se["bayes"] + 1e-12


def test_composition_fast() -> None:
    _check(0.1, 120, 100.0, 1.0, 300, 4)


def test_bayes_alone_breaks_the_frequentist_guarantee() -> None:
    res = simulate_rules(
        NormalPrior(mean=-0.01, sd=0.03),
        0.0,
        LossModel(wrong_promote=1.0, wrong_block=1.0),
        alpha=0.05,
        n=300,
        reps=120,
        seed=8,
    )
    assert res.boundary_promote_rate["bayes"] > 0.3
    assert res.boundary_promote_rate["composed"] <= 0.05 + 3 * math.sqrt(0.05 * 0.95 / 120)


@pytest.mark.slow
@pytest.mark.parametrize(("c_a", "c_b"), [(100.0, 1.0), (10.0, 1.0), (1.0, 1.0), (1.0, 10.0)])
def test_composition_slow(c_a: float, c_b: float) -> None:
    _check(0.1, 1000, c_a, c_b, 500, 40)
