"""Variance-aware bets for the paired regression test (docs/math/07).

Every strategy here gives a valid e-process for the same composite null as
``sequential.BettingEProcess`` (observations in [-1, 1], conditional mean on the
null side of ``m``); they differ only in power.

* ``"agrapa"``: the default bet of ``sequential.py`` (max bet 0.5).
* ``"agrapa_wide"``: the same bet allowed up to 0.95 of the largest valid bet.
* ``"plugin"``: the predictable plug-in bet of Waudby-Smith and Ramdas (2023,
  "Estimating means of bounded random variables by betting", JRSS-B),
  ``lambda_t = sqrt(2 log(1/a) / (sigma2_{t-1} t log(1 + t)))``: scales with the
  running variance only, never with the running mean.
* ``"mixture"``: the average of constant-bet wealth processes over a geometric grid
  of bets (Proposition 7.1). An average of test supermartingales is one, and its log
  wealth is within ``log J`` of the best bet on the grid on every path.

``oracle_growth`` computes ``g* = max_lambda mean log(1 + lambda (x - m))`` over
``[0, 1/(1+m)]``, the best constant-bet growth rate on a sample. Proposition 7.2
shows that ``g*`` is the KL divergence from the sample's law to a law in the null,
so ``kl(1 - beta || alpha) / g*`` cases are needed by any level-``alpha`` test that
is valid for every law on [-1, 1] with mean ``<= m`` (``required_cases_lower_bound``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .sequential import Alternative, BettingEProcess, safe_exp

BetStrategy = Literal["agrapa", "agrapa_wide", "plugin", "mixture"]
BET_STRATEGIES: tuple[BetStrategy, ...] = ("agrapa", "agrapa_wide", "plugin", "mixture")


class EProcess(Protocol):
    @property
    def n(self) -> int: ...

    @property
    def e_value(self) -> float: ...

    @property
    def max_e_value(self) -> float: ...

    def update(self, x: float) -> float: ...

    def rejected(self, alpha: float) -> bool: ...


def _check_obs(x: float) -> float:
    if math.isnan(x) or not -1.0 - 1e-12 <= x <= 1.0 + 1e-12:
        raise ValueError(f"observation {x} outside [-1, 1]; normalise by the cost cap")
    return max(-1.0, min(1.0, x))


def _check_common(null_mean: float, alternative: str, max_bet: float) -> int:
    if not -1.0 < null_mean < 1.0:
        raise ValueError("null_mean must lie strictly inside (-1, 1)")
    if alternative not in ("greater", "less"):
        raise ValueError("alternative must be 'greater' or 'less'")
    if not 0.0 < max_bet < 1.0:
        raise ValueError("max_bet must lie in (0, 1)")
    return 1 if alternative == "greater" else -1


def _logmeanexp(xs: Sequence[float]) -> float:
    top = max(xs)
    return top + math.log(math.fsum(math.exp(x - top) for x in xs) / len(xs))


@dataclass
class GridMixtureEProcess:
    """Average of constant-bet e-processes, bets ``lambda_max * ratio**j``, ``j < size``.

    ``lambda_max = max_bet / (1 + s m)`` is the largest bet keeping every factor at
    least ``1 - max_bet``. Weights are uniform.
    """

    null_mean: float
    alternative: Alternative
    max_bet: float = 0.95
    ratio: float = 0.5
    size: int = 14

    n: int = field(default=0, init=False)
    log_wealth: float = field(default=0.0, init=False)
    max_log_wealth: float = field(default=0.0, init=False)
    _logs: list[float] = field(default_factory=list, init=False, repr=False)
    _bets: list[float] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        s = _check_common(self.null_mean, self.alternative, self.max_bet)
        if not 0.0 < self.ratio < 1.0:
            raise ValueError("ratio must lie in (0, 1)")
        if self.size < 1:
            raise ValueError("size must be at least 1")
        top = self.max_bet / (1.0 + s * self.null_mean)
        self._bets = [top * self.ratio**j for j in range(self.size)]
        self._logs = [0.0] * self.size
        self._sign = s

    @property
    def bets(self) -> tuple[float, ...]:
        return tuple(self._bets)

    def update(self, x: float) -> float:
        y = self._sign * (_check_obs(x) - self.null_mean)
        for j, lam in enumerate(self._bets):
            self._logs[j] += math.log1p(lam * y)
        self.n += 1
        self.log_wealth = _logmeanexp(self._logs)
        self.max_log_wealth = max(self.max_log_wealth, self.log_wealth)
        return self.e_value

    @property
    def e_value(self) -> float:
        return safe_exp(self.log_wealth)

    @property
    def max_e_value(self) -> float:
        return safe_exp(self.max_log_wealth)

    def best_component(self) -> tuple[float, float]:
        """``(bet, log wealth)`` of the best constant bet so far."""
        j = max(range(self.size), key=lambda i: self._logs[i])
        return self._bets[j], self._logs[j]

    def rejected(self, alpha: float) -> bool:
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha must lie in (0, 1)")
        return self.max_log_wealth >= math.log(1.0 / alpha) - 1e-12


@dataclass
class PlugInEProcess:
    """Predictable plug-in bet: ``min(lambda_max, sqrt(2 log(1/a) / (s2 t log(1+t))))``.

    ``s2`` is the running variance of ``Y`` with one pseudo-observation carrying
    ``prior_variance``; ``a`` is the level the bet is tuned for (validity holds for
    every level, because the bet is non-negative and predictable).
    """

    null_mean: float
    alternative: Alternative
    bet_alpha: float = 0.05
    max_bet: float = 0.95
    prior_variance: float = 0.25

    n: int = field(default=0, init=False)
    log_wealth: float = field(default=0.0, init=False)
    max_log_wealth: float = field(default=0.0, init=False)
    _sum: float = field(default=0.0, init=False, repr=False)
    _ssd: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._sign = _check_common(self.null_mean, self.alternative, self.max_bet)
        if not 0.0 < self.bet_alpha < 1.0:
            raise ValueError("bet_alpha must lie in (0, 1)")
        if self.prior_variance <= 0:
            raise ValueError("prior_variance must be positive")
        self._top = self.max_bet / (1.0 + self._sign * self.null_mean)

    def next_bet(self) -> float:
        t = self.n + 1
        s2 = (self.prior_variance + self._ssd) / t
        lam = math.sqrt(2.0 * math.log(1.0 / self.bet_alpha) / (s2 * t * math.log1p(t)))
        return min(self._top, lam)

    def update(self, x: float) -> float:
        y = self._sign * (_check_obs(x) - self.null_mean)
        lam = self.next_bet()
        self.log_wealth += math.log1p(lam * y)
        self.max_log_wealth = max(self.max_log_wealth, self.log_wealth)
        mean_prev = self._sum / (self.n + 1)
        self._ssd += (y - mean_prev) ** 2
        self._sum += y
        self.n += 1
        return self.e_value

    @property
    def e_value(self) -> float:
        return safe_exp(self.log_wealth)

    @property
    def max_e_value(self) -> float:
        return safe_exp(self.max_log_wealth)

    def rejected(self, alpha: float) -> bool:
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha must lie in (0, 1)")
        return self.max_log_wealth >= math.log(1.0 / alpha) - 1e-12


def make_e_process(strategy: BetStrategy, null_mean: float, alternative: Alternative) -> EProcess:
    """A fresh e-process for ``null_mean`` with the named betting strategy."""
    if strategy == "agrapa":
        return BettingEProcess(null_mean, alternative)
    if strategy == "agrapa_wide":
        return BettingEProcess(null_mean, alternative, max_bet=0.95)
    if strategy == "plugin":
        return PlugInEProcess(null_mean, alternative)
    if strategy == "mixture":
        return GridMixtureEProcess(null_mean, alternative)
    raise ValueError(f"unknown strategy {strategy!r}")


def growth(xs: Sequence[float], null_mean: float, bet: float) -> float:
    """``mean log(1 + bet (x - m))`` on a sample (``-inf`` if a factor is not positive)."""
    if not xs:
        raise ValueError("empty sample")
    total = 0.0
    for x in xs:
        f = 1.0 + bet * (x - null_mean)
        if f <= 0.0:
            return -math.inf
        total += math.log(f)
    return total / len(xs)


def oracle_growth(xs: Sequence[float], null_mean: float) -> tuple[float, float]:
    """``(g*, lambda*)``: the best constant bet for "mean > m" on this sample.

    ``g(lambda) = mean log(1 + lambda (x - m))`` is concave on the closed interval
    ``[0, 1/(1+m)]`` (``-inf`` at the right end if some ``x = -1``). Its maximiser is
    the right end when the derivative ``mean (x - m)/(1 + lambda (x - m))`` is still
    non-negative there, else found by bisection on the derivative.
    """
    if not -1.0 < null_mean < 1.0:
        raise ValueError("null_mean must lie strictly inside (-1, 1)")
    ys = [_check_obs(x) - null_mean for x in xs]
    if not ys:
        raise ValueError("empty sample")
    top = 1.0 / (1.0 + null_mean)

    def deriv(lam: float) -> float:
        return math.fsum(y / (1.0 + lam * y) for y in ys) / len(ys)

    if deriv(0.0) <= 0.0:
        return 0.0, 0.0
    if all(1.0 + top * y > 0.0 for y in ys) and deriv(top) >= 0.0:
        return growth(xs, null_mean, top), top
    lo, hi = 0.0, top
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if 1.0 + mid * min(ys) > 0.0 and deriv(mid) > 0.0:
            lo = mid
        else:
            hi = mid
    return growth(xs, null_mean, lo), lo


def binary_kl(p: float, q: float) -> float:
    """``kl(p || q)`` for Bernoulli laws."""
    if not (0.0 <= p <= 1.0 and 0.0 < q < 1.0):
        raise ValueError("need 0 <= p <= 1 and 0 < q < 1")
    out = 0.0
    if p > 0.0:
        out += p * math.log(p / q)
    if p < 1.0:
        out += (1.0 - p) * math.log((1.0 - p) / (1.0 - q))
    return out


def required_cases_lower_bound(g_star: float, alpha: float, beta: float) -> float:
    """Proposition 7.2: ``kl(1 - beta || alpha) / g*`` (``inf`` if ``g* <= 0``)."""
    if not 0.0 < alpha < 1.0 - beta < 1.0:
        raise ValueError("need 0 < alpha < 1 - beta < 1")
    if g_star <= 0.0:
        return math.inf
    return binary_kl(1.0 - beta, alpha) / g_star


__all__ = [
    "BET_STRATEGIES",
    "BetStrategy",
    "EProcess",
    "GridMixtureEProcess",
    "PlugInEProcess",
    "binary_kl",
    "growth",
    "make_e_process",
    "oracle_growth",
    "required_cases_lower_bound",
]
