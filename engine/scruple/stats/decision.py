"""Cost-weighted asymmetric promote/block rule, composed with the frequentist test.

Setting
-------
``theta`` is the true mean per-case loss difference (candidate minus baseline, USD
minor units; positive = worse) and ``delta`` the tolerance. Two actions: promote or
block. Two loss models (``LossModel``):

* ``zero_one``: promoting when ``theta > delta`` costs ``c_A`` (a wrong approval);
  blocking when ``theta <= delta`` costs ``c_B`` (a wrong block); correct actions cost 0.
* ``linear``: promoting costs ``c_A V (theta - delta)^+`` (excess loss over ``V`` cases
  until the next review) and blocking costs ``c_B V (delta - theta)^+`` (forgone savings).

Proposition 1 (Bayes rule). Under a posterior ``Pi`` for ``theta``, the action
minimising posterior expected loss is: promote iff ``rho_P <= rho_B`` where
``rho_P = E_Pi[loss(promote, theta)]`` and ``rho_B = E_Pi[loss(block, theta)]``.
For ``zero_one`` this is ``Pi(theta > delta) <= p* = c_B / (c_A + c_B)``; for
``linear`` it is ``c_A E(theta - delta)^+ <= c_B E(delta - theta)^+``. With a normal
posterior ``N(mu, s^2)`` and ``z = (mu - delta)/s``,
``E(theta - delta)^+ = (mu - delta) Phi(z) + s phi(z)`` and
``E(delta - theta)^+ = (delta - mu) Phi(-z) + s phi(z)``. Proof: the Bayes risk of a
rule ``phi`` is ``int [phi(x) rho_P(x) + (1 - phi(x)) rho_B(x)] dm(x)`` (Fubini), which
is minimised pointwise. Standard (e.g. Berger 1985, "Statistical Decision Theory and
Bayesian Analysis").

The posterior here is the conjugate normal approximation: estimate ``theta_hat`` (the
mean paired difference) with standard error ``se``, prior ``N(mu0, tau^2)``. This is an
approximation (CLT for the mean); the frequentist part below is exact.

Proposition 2 (composition). Let ``E`` be an e-value (e.g. a stopped betting
e-process) for ``H0: theta >= delta`` and ``A = {E >= 1/alpha}``. The composed rule
promotes iff ``x in A`` and the Bayes rule promotes. Then

(i) for every ``theta >= delta``, ``P_theta(promote) <= P_theta(E >= 1/alpha) <= alpha``
    by Markov, whatever the prior and whether or not the normal approximation holds;
(ii) among all rules ``phi`` with ``phi <= 1_A`` (rules that never promote without the
    frequentist certificate), the composed rule has the smallest Bayes risk, by the
    same pointwise argument restricted to ``A``;
(iii) in particular its Bayes risk is at most that of the frequentist rule ``1_A``
    alone, and the price of the guarantee relative to the unconstrained Bayes rule is
    ``int_{A^c} (rho_B - rho_P)^+ dm``, which the simulation measures.

Under ``zero_one`` losses, (i) bounds the frequentist expected cost of wrong approvals
by ``alpha c_A`` for every harmful ``theta``, so a cost budget ``b`` fixes
``alpha = b / c_A`` (``alpha_for_budget``). Grünwald (2024, "Beyond Neyman-Pearson:
e-values enable hypothesis testing with a data-driven alpha", PNAS) shows e-values also
allow choosing ``alpha`` after seeing the data; we do not use that extension here.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .sequential import BettingEProcess
from .simulate import draw, ternary_with_mean

_N = NormalDist()


class NormalPrior(BaseModel):
    """Prior on the mean per-case loss difference (USD minor units)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mean: float = 0.0
    sd: float = Field(gt=0.0)


class LossModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["zero_one", "linear"] = "zero_one"
    wrong_promote: float = Field(gt=0.0)  # c_A
    wrong_block: float = Field(gt=0.0)  # c_B
    volume: float = Field(default=1.0, gt=0.0)  # V: cases affected until next review (linear)


class Posterior(BaseModel):
    model_config = ConfigDict(frozen=True)

    mean: float
    sd: float


class ComposedDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    promote: bool
    bayes_promote: bool
    certified: bool  # E >= 1/alpha for H0: theta >= delta
    e_value: float
    alpha: float
    prob_harm: float  # posterior P(theta > delta)
    risk_promote: float
    risk_block: float


def normal_posterior(prior: NormalPrior, estimate: float, se: float) -> Posterior:
    """Conjugate update of ``N(mu0, tau^2)`` with ``theta_hat ~ N(theta, se^2)``."""
    if se <= 0:
        raise ValueError("standard error must be positive")
    w0, w1 = 1.0 / prior.sd**2, 1.0 / se**2
    var = 1.0 / (w0 + w1)
    return Posterior(mean=var * (w0 * prior.mean + w1 * estimate), sd=math.sqrt(var))


def mean_and_se(diffs: list[float] | tuple[float, ...]) -> tuple[float, float]:
    """Sample mean and standard error (floored so a constant sample still has se > 0)."""
    n = len(diffs)
    if n < 2:
        raise ValueError("need at least two paired differences")
    mean = math.fsum(diffs) / n
    var = math.fsum((d - mean) ** 2 for d in diffs) / (n - 1)
    return mean, max(math.sqrt(var / n), 1e-9)


def bayes_threshold(loss: LossModel) -> float:
    """``p* = c_B / (c_A + c_B)``: promote iff posterior P(harm) is at most this."""
    return loss.wrong_block / (loss.wrong_promote + loss.wrong_block)


def posterior_risks(post: Posterior, tolerance: float, loss: LossModel) -> tuple[float, float]:
    """``(rho_P, rho_B)``: posterior expected loss of promoting and of blocking."""
    z = (post.mean - tolerance) / post.sd
    if loss.kind == "zero_one":
        p_harm = 1.0 - _N.cdf(-z)
        return loss.wrong_promote * p_harm, loss.wrong_block * (1.0 - p_harm)
    excess = (post.mean - tolerance) * _N.cdf(z) + post.sd * _N.pdf(z)
    saving = (tolerance - post.mean) * _N.cdf(-z) + post.sd * _N.pdf(z)
    v = loss.volume
    return loss.wrong_promote * v * excess, loss.wrong_block * v * saving


def bayes_promote(post: Posterior, tolerance: float, loss: LossModel) -> bool:
    rho_p, rho_b = posterior_risks(post, tolerance, loss)
    return rho_p <= rho_b


def composed_decision(
    e_value: float, alpha: float, post: Posterior, tolerance: float, loss: LossModel
) -> ComposedDecision:
    """Promote iff the e-value certifies ``theta < delta`` at ``alpha`` AND Bayes agrees."""
    if not 0 < alpha < 1:
        raise ValueError("alpha must lie in (0, 1)")
    if e_value < 0:
        raise ValueError("e-value must be non-negative")
    rho_p, rho_b = posterior_risks(post, tolerance, loss)
    certified = e_value >= 1.0 / alpha
    bayes = rho_p <= rho_b
    return ComposedDecision(
        promote=certified and bayes,
        bayes_promote=bayes,
        certified=certified,
        e_value=e_value,
        alpha=alpha,
        prob_harm=1.0 - _N.cdf((tolerance - post.mean) / post.sd),
        risk_promote=rho_p,
        risk_block=rho_b,
    )


def alpha_for_budget(loss: LossModel, budget: float) -> float:
    """``alpha = budget / c_A``: caps the expected wrong-approval cost at ``budget``."""
    if loss.kind != "zero_one":
        raise ValueError("budget calibration is defined for zero_one losses")
    a = budget / loss.wrong_promote
    if not 0 < a < 1:
        raise ValueError("budget must lie strictly between 0 and the wrong-approval cost")
    return a


# --- simulation (synthetic; normalised units) --------------------------------------------


class RuleStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    bayes_risk: dict[str, float]  # mean realised loss per rule, theta drawn from the prior
    boundary_promote_rate: dict[str, float]  # P(promote | theta = delta) per rule
    risk_diff_se: dict[str, float]  # Monte Carlo SE of (rule risk - composed risk), paired
    posterior_risk: dict[str, float]  # mean posterior expected loss of the chosen action


def realised_loss(promote: bool, theta: float, tolerance: float, loss: LossModel) -> float:
    if loss.kind == "zero_one":
        if promote:
            return loss.wrong_promote if theta > tolerance else 0.0
        return loss.wrong_block if theta <= tolerance else 0.0
    if promote:
        return loss.wrong_promote * loss.volume * max(theta - tolerance, 0.0)
    return loss.wrong_block * loss.volume * max(tolerance - theta, 0.0)


def simulate_rules(
    prior: NormalPrior,
    tolerance: float,
    loss: LossModel,
    *,
    alpha: float,
    n: int,
    reps: int,
    seed: int,
    p_changed: float = 0.3,
    magnitude: float = 0.8,
) -> RuleStats:
    """Compare Bayes-only, frequentist-only and composed rules (normalised units).

    ``theta`` is drawn from the prior (truncated to what the ternary law can reach),
    then ``n`` paired differences from a ternary law with mean ``theta``. A second
    batch of ``reps`` runs fixes ``theta = delta`` to measure the boundary error.
    """
    rng = random.Random(seed)
    reach = 0.95 * p_changed * magnitude
    names = ("bayes", "frequentist", "composed", "always_block")
    totals = dict.fromkeys(names, 0.0)
    post_totals = dict.fromkeys(names, 0.0)
    boundary = dict.fromkeys(names, 0)
    diff_sq = dict.fromkeys(names, 0.0)
    diff_sum = dict.fromkeys(names, 0.0)
    for rep in range(2 * reps):
        at_boundary = rep >= reps
        if at_boundary:
            theta = tolerance
        else:
            theta = max(-reach, min(reach, rng.gauss(prior.mean, prior.sd)))
        xs = draw(ternary_with_mean(theta, p_changed, magnitude), n, rng)
        ep = BettingEProcess(tolerance, "less")
        ep.extend(xs)
        est, se = mean_and_se(xs)
        post = normal_posterior(prior, est, se)
        d = composed_decision(ep.e_value, alpha, post, tolerance, loss)
        choice = {
            "bayes": d.bayes_promote,
            "frequentist": d.certified,
            "composed": d.promote,
            "always_block": False,
        }
        if at_boundary:
            for name in names:
                boundary[name] += choice[name]
            continue
        losses = {k: realised_loss(c, theta, tolerance, loss) for k, c in choice.items()}
        for name in names:
            totals[name] += losses[name]
            post_totals[name] += d.risk_promote if choice[name] else d.risk_block
            diff = losses[name] - losses["composed"]
            diff_sum[name] += diff
            diff_sq[name] += diff * diff
    diff_se: dict[str, float] = {}
    for k in names:
        mean = diff_sum[k] / reps
        var = max(diff_sq[k] / reps - mean * mean, 0.0)
        diff_se[k] = math.sqrt(var / reps)
    return RuleStats(
        bayes_risk={k: v / reps for k, v in totals.items()},
        boundary_promote_rate={k: v / reps for k, v in boundary.items()},
        risk_diff_se=diff_se,
        posterior_risk={k: v / reps for k, v in post_totals.items()},
    )
