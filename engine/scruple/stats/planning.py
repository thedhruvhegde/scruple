"""Sample-size and power planning for the stratified betting test.

Question: given stratum frequencies ``pi_k``, per-case cost caps ``c_k``, a tolerance
``t_k`` and a minimum detectable regression ``Delta_k`` (all in USD minor units), how
many held-out cases (or canary days) are needed so that a regression of at least
``Delta_k`` in stratum ``k`` is flagged with probability at least ``1 - beta``, while
the family-wise false-flag rate stays at most ``alpha``?

Derivation (fixed-bet e-test, conservative bound solved exactly)
-------------------------------------------------------
Normalise: ``X = (C - B)/c_k`` in [-1, 1], null mean ``m = t_k/c_k``, alternative mean
``mu = m + d`` with ``d = Delta_k/c_k > 0``, ``sigma^2 = Var X``. Let ``Y = X - m`` so
``Y >= -(1 + m)``, ``E Y = d`` and ``E Y^2 = sigma^2 + d^2 =: M2``. With a constant bet
``lambda`` in ``(0, 1/(1+m))`` the log-wealth ``S_n = sum log(1 + lambda Y_i)`` has i.i.d.
increments.

Lemma 1 (log lower bound). For ``a in [0, 1)`` and ``x >= -a``,
``log(1 + x) >= x - x^2 / (2 (1 - a))``. Proof: ``f(x) = log(1+x) - x + x^2/(2(1-a))``
has ``f(0) = 0`` and ``f'(x) = x (1/(1-a) - 1/(1+x))``, which is ``<= 0`` on
``[-a, 0]`` and ``>= 0`` on ``[0, inf)``; so ``f >= 0``.

Applying it with ``x = lambda Y`` and ``a = lambda (1 + m)`` gives the growth bound

    g(lambda) := E log(1 + lambda Y) >= lambda d - lambda^2 M2 / (2 (1 - lambda (1 + m))).

Concentration of ``S_n`` (the default, ``method="bernstein"``). Write
``Z = log(1 + lambda Y)`` and ``a = lambda (1 + m)``. Since ``log(1 + x) <= x`` and
``log(1 + x) >= x / (1 - a)`` for ``x in [-a, 0]``, ``|Z| <= lambda |Y| / (1 - a)``, so
``Var Z <= v := lambda^2 M2 / (1 - a)^2``; and ``E Z <= lambda d`` so the lower deviation
``E Z - Z <= b := lambda d - log(1 - a)``. Bernstein's inequality for i.i.d. variables
whose deviations below the mean are at most ``b`` gives
``P(S_n <= n E Z - eps) <= beta`` with
``eps = l b / 3 + sqrt((l b / 3)^2 + 2 l n v)``, ``l = log(1/beta)``. The test rejects
at level ``alpha_k`` once ``S_t >= L := log(1/alpha_k)``; because
``sup_{t<=n} S_t >= S_n`` and ``E Z >= g(lambda)``, power by ``n`` is at least
``1 - beta`` whenever

    n g(lambda) - eps(n, lambda) >= L.

We take the smallest such ``n`` (bisection on ``n``) for the best ``lambda`` (grid plus
golden section). ``method="hoeffding"`` replaces ``eps`` by ``R sqrt(n l / 2)`` with
``R = log((1 + lambda (1 - m)) / (1 - lambda (1 + m)))`` the increment range (Hoeffding
1963); it ignores the variance and is far more conservative when most cases are
unchanged. Both bounds use only the mean gap, a variance bound and the cap, so they
hold for every distribution with those moments.

An approximate (CLT) plan uses ``eps = z_{1-beta} sqrt(n) lambda sigma``
(the increment's standard deviation is about ``lambda sigma``). It is not a guarantee;
the simulation in tests and scripts shows how close it is.

Stratum counts are random: with ``N`` total cases, ``n_k ~ Binomial(N, pi_k)``. We take
the smallest ``N`` with ``P(Binomial(N, pi_k) >= n_k*) >= 1 - gamma`` (computed exactly),
so the per-stratum power is at least ``(1 - beta)(1 - gamma)`` (the counts are
independent of the per-stratum outcomes for i.i.d. traffic, and power is
non-decreasing in ``n``).

Error-budget allocation. With e-Bonferroni weights the per-stratum level is
``alpha_k = alpha w_k / K`` with ``sum_k w_k = K``, i.e. ``sum_k alpha_k = alpha``. For a
total of ``N`` cases, stratum ``k`` reaches log-wealth ``L_k(N)`` with power
``1 - beta``; the smallest feasible ``N`` satisfies ``sum_k exp(-L_k(N)) <= alpha``,
found by bisection. Rare strata get a larger share of ``alpha``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from statistics import NormalDist
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .sequential import BettingEProcess
from .simulate import draw, ternary_with_moments

Method = Literal["bernstein", "hoeffding", "clt"]


class StratumSpec(BaseModel):
    """One case type to plan for. Money in USD minor units."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    frequency: float = Field(gt=0.0, le=1.0)
    cost_cap: float = Field(gt=0.0)
    min_detectable_regression: float = Field(gt=0.0)
    tolerance: float = 0.0
    sd: float | None = Field(default=None, gt=0.0)  # per-case sd of C - B; None = worst case

    @model_validator(mode="after")
    def _within_cap(self) -> StratumSpec:
        if abs(self.tolerance) + self.min_detectable_regression >= self.cost_cap:
            raise ValueError("tolerance + min_detectable_regression must be below the cost cap")
        return self

    @property
    def null_mean(self) -> float:
        return self.tolerance / self.cost_cap

    @property
    def gap(self) -> float:
        return self.min_detectable_regression / self.cost_cap

    @property
    def sigma(self) -> float:
        mu = self.null_mean + self.gap
        worst = math.sqrt(max(1.0 - mu * mu, 0.0))
        return worst if self.sd is None else min(self.sd / self.cost_cap, worst)


class StratumPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    alpha: float  # per-stratum level alpha_k
    bet: float  # constant bet lambda (normalised units)
    stratum_cases: int  # n_k*: cases of this type needed
    total_cases: int  # N_k: total traffic so that n_k >= n_k* w.p. >= 1 - gamma


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: Method
    alpha: float
    beta: float
    gamma: float
    strata: tuple[StratumPlan, ...]
    total_cases: int
    days: float | None  # total_cases / (daily_cases * canary_fraction), if given
    binding_stratum: str


# --- single-stratum maths ----------------------------------------------------------------


def growth_lower_bound(lam: float, gap: float, sigma: float, null_mean: float) -> float:
    """Lemma 1: ``E log(1 + lam Y) >= lam d - lam^2 M2 / (2 (1 - lam (1 + m)))``."""
    a = lam * (1.0 + null_mean)
    if not 0.0 <= a < 1.0:
        raise ValueError("bet too large for this null mean")
    m2 = sigma * sigma + gap * gap
    return lam * gap - lam * lam * m2 / (2.0 * (1.0 - a))


def increment_range(lam: float, null_mean: float) -> float:
    return math.log1p(lam * (1.0 - null_mean)) - math.log1p(-lam * (1.0 + null_mean))


def level_reached(lam: float, spec: StratumSpec, n: int, beta: float, method: Method) -> float:
    """A log-wealth level ``S_n`` exceeds with probability ``>= 1 - beta`` (fixed bet).

    ``n g - eps`` where ``g`` is the growth bound and ``eps`` the lower deviation from
    Bernstein, Hoeffding, or (approximately) the CLT. See the module docstring.
    """
    if n <= 0:
        return -math.inf
    g = growth_lower_bound(lam, spec.gap, spec.sigma, spec.null_mean)
    ell = math.log(1.0 / beta)
    if method == "hoeffding":
        eps = increment_range(lam, spec.null_mean) * math.sqrt(n * ell / 2.0)
    elif method == "bernstein":
        a = lam * (1.0 + spec.null_mean)
        var = lam * lam * (spec.sigma**2 + spec.gap**2) / (1.0 - a) ** 2
        b = lam * spec.gap - math.log1p(-a)
        eps = ell * b / 3.0 + math.sqrt((ell * b / 3.0) ** 2 + 2.0 * ell * n * var)
    else:
        eps = NormalDist().inv_cdf(1.0 - beta) * lam * spec.sigma * math.sqrt(n)
    return n * g - eps


def best_level(spec: StratumSpec, n: int, beta: float, method: Method) -> tuple[float, float]:
    """``(L, bet)``: the best fixed bet for ``n`` cases and the level it reaches."""
    lam_max = 0.99 / (1.0 + spec.null_mean)
    grid = [lam_max * i / 200 for i in range(1, 200)]
    vals = [level_reached(lam, spec, n, beta, method) for lam in grid]
    i = max(range(len(grid)), key=vals.__getitem__)
    lo, hi = grid[max(i - 1, 0)], grid[min(i + 1, len(grid) - 1)]
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    for _ in range(40):  # golden section on a concave-ish objective
        x1, x2 = hi - phi * (hi - lo), lo + phi * (hi - lo)
        if level_reached(x1, spec, n, beta, method) >= level_reached(x2, spec, n, beta, method):
            hi = x2
        else:
            lo = x1
    lam = (lo + hi) / 2.0
    best = level_reached(lam, spec, n, beta, method)
    return (best, lam) if best >= vals[i] else (vals[i], grid[i])


def max_log_threshold(
    spec: StratumSpec, n: int, beta: float, method: Method = "bernstein"
) -> float:
    """Largest ``L = log(1/alpha_k)`` reached with power ``1 - beta`` by ``n`` cases."""
    return best_level(spec, n, beta, method)[0] if n > 0 else -math.inf


def stratum_cases_needed(
    spec: StratumSpec, alpha_k: float, beta: float, method: Method = "bernstein"
) -> tuple[int, float]:
    """``(n_k*, bet)``: fewest stratum cases giving the fixed-bet test power ``>= 1 - beta``."""
    _check_level(alpha_k, "alpha_k")
    _check_level(beta, "beta")
    target = math.log(1.0 / alpha_k)
    hi = 1
    while max_log_threshold(spec, hi, beta, method) < target:
        hi *= 2
        if hi > 2**40:
            raise ValueError("no feasible sample size (gap too small for the cap)")
    lo = hi // 2  # lo fails (or is 0), hi succeeds
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if max_log_threshold(spec, mid, beta, method) >= target:
            hi = mid
        else:
            lo = mid
    return hi, best_level(spec, hi, beta, method)[1]


# --- binomial counts (exact) --------------------------------------------------------------


def _log_pmf(j: int, n: int, lp: float, lq: float) -> float:
    return math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * lp + (n - j) * lq


def binomial_sf(k: int, n: int, p: float) -> float:
    """``P(Binomial(n, p) >= k)``.

    Sums the tail that does not contain the mean, walking away from ``k`` with the
    pmf ratio and stopping once terms fall below ``1e-17`` of the running sum (the
    remaining terms decrease geometrically, so the truncation error is negligible).
    """
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p >= 1.0:
        return 1.0
    if p <= 0.0:
        return 0.0
    lp, lq = math.log(p), math.log1p(-p)
    ratio_up = p / (1.0 - p)
    if k - 1 < n * p:  # lower tail P(X <= k-1) is the small one
        j = k - 1
        term = math.exp(_log_pmf(j, n, lp, lq))
        total = 0.0
        while j >= 0 and term > 0.0:
            total += term
            if term < 1e-17 * total:
                break
            term *= j / ((n - j + 1) * ratio_up)
            j -= 1
        return max(0.0, min(1.0, 1.0 - total))
    j = k
    term = math.exp(_log_pmf(j, n, lp, lq))
    total = 0.0
    while j <= n and term > 0.0:
        total += term
        if term < 1e-17 * total:
            break
        term *= (n - j) / (j + 1) * ratio_up
        j += 1
    return max(0.0, min(1.0, total))


def total_cases_for_count(needed: int, frequency: float, gamma: float) -> int:
    """Smallest ``N`` with ``P(Binomial(N, frequency) >= needed) >= 1 - gamma``."""
    _check_level(gamma, "gamma")
    if not 0 < frequency <= 1:
        raise ValueError("frequency must lie in (0, 1]")
    if needed <= 0:
        return 0
    lo, hi = needed - 1, max(needed, math.ceil(needed / frequency))
    while binomial_sf(needed, hi, frequency) < 1.0 - gamma:
        hi *= 2
    while hi - lo > 1:  # invariant: lo fails, hi succeeds
        mid = (lo + hi) // 2
        if binomial_sf(needed, mid, frequency) >= 1.0 - gamma:
            hi = mid
        else:
            lo = mid
    return hi


def guaranteed_count(total: int, frequency: float, gamma: float) -> int:
    """Largest ``n`` with ``P(Binomial(total, frequency) >= n) >= 1 - gamma``."""
    lo, hi = 0, total + 1  # lo succeeds, hi fails
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if binomial_sf(mid, total, frequency) >= 1.0 - gamma:
            lo = mid
        else:
            hi = mid
    return lo


# --- full plan ------------------------------------------------------------------------------


def plan(
    strata: Sequence[StratumSpec],
    *,
    alpha: float = 0.05,
    beta: float = 0.2,
    gamma: float = 0.05,
    method: Method = "bernstein",
    allocation: Literal["uniform", "optimised"] = "uniform",
    daily_cases: float | None = None,
    canary_fraction: float = 1.0,
) -> Plan:
    """Cases (and days) needed to flag each stratum's minimum regression.

    ``uniform`` gives every stratum ``alpha / K``; ``optimised`` finds the smallest total
    with ``sum_k exp(-L_k(N)) <= alpha`` and sets ``alpha_k = exp(-L_k(N))`` (rescaled to
    use the whole budget).
    """
    for name, v in (("alpha", alpha), ("beta", beta), ("gamma", gamma)):
        _check_level(v, name)
    if not strata:
        raise ValueError("at least one stratum")
    if len({s.name for s in strata}) != len(strata):
        raise ValueError("stratum names must be unique")
    k = len(strata)
    if allocation == "uniform":
        alphas = [alpha / k] * k
    else:
        alphas = _optimised_alphas(strata, alpha, beta, gamma, method)
    plans: list[StratumPlan] = []
    for spec, a_k in zip(strata, alphas, strict=True):
        n_k, lam = stratum_cases_needed(spec, a_k, beta, method)
        plans.append(
            StratumPlan(
                name=spec.name,
                alpha=a_k,
                bet=lam,
                stratum_cases=n_k,
                total_cases=total_cases_for_count(n_k, spec.frequency, gamma),
            )
        )
    binding = max(plans, key=lambda p: p.total_cases)
    days = None
    if daily_cases is not None:
        if daily_cases <= 0 or not 0 < canary_fraction <= 1:
            raise ValueError("daily_cases must be positive and canary_fraction in (0, 1]")
        days = binding.total_cases / (daily_cases * canary_fraction)
    return Plan(
        method=method,
        alpha=alpha,
        beta=beta,
        gamma=gamma,
        strata=tuple(plans),
        total_cases=binding.total_cases,
        days=days,
        binding_stratum=binding.name,
    )


def _optimised_alphas(
    strata: Sequence[StratumSpec], alpha: float, beta: float, gamma: float, method: Method
) -> list[float]:
    def needed(total: int) -> list[float]:
        return [
            math.exp(
                -min(
                    max_log_threshold(s, guaranteed_count(total, s.frequency, gamma), beta, method),
                    700.0,
                )
            )
            for s in strata
        ]

    def feasible(total: int) -> bool:
        return math.fsum(needed(total)) <= alpha

    hi = max(16, min(math.ceil(1.0 / s.frequency) for s in strata))
    while not feasible(hi):
        hi *= 2
    lo = hi // 2 if hi > 16 else 0
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if feasible(mid):
            hi = mid
        else:
            lo = mid
    req = needed(hi)
    scale = alpha / math.fsum(req)  # >= 1: hand out the unused budget proportionally
    return [r * scale for r in req]


def _check_level(v: float, name: str) -> None:
    if not 0.0 < v < 1.0:
        raise ValueError(f"{name} must lie in (0, 1)")


def simulate_power(
    spec: StratumSpec,
    n: int,
    alpha_k: float,
    *,
    bet: float | None,
    reps: int,
    seed: int,
) -> float:
    """Monte Carlo power by ``n`` stratum cases under a ternary law with the spec's
    mean and standard deviation (synthetic). ``bet=None`` uses the adaptive aGRAPA bet."""
    law = ternary_with_moments(spec.null_mean + spec.gap, spec.sigma)
    rng = random.Random(seed)
    hits = 0
    for _ in range(reps):
        if bet is None:
            ep = BettingEProcess(spec.null_mean, "greater")
        else:
            ep = BettingEProcess(spec.null_mean, "greater", "fixed", fixed_bet=bet)
        ep.extend(draw(law, n, rng))
        hits += ep.rejected(alpha_k)
    return hits / reps
