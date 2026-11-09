"""Anytime-valid paired test for loss regression (betting e-processes).

Setting
-------
Case ``i`` has a baseline loss ``B_i`` and a candidate loss ``C_i`` (USD minor units,
e.g. regret). The operator declares a per-case cost cap ``c`` with
``|C_i - B_i| <= c``. The normalised paired difference is

    X_i = (C_i - B_i) / c  in [-1, 1],   positive = candidate worse.

Let ``mu_i = E[X_i | F_{i-1}]`` (``F`` is everything observed before case ``i``; for
i.i.d. cases ``mu_i = mu``). Two one-sided composite nulls matter:

* ``alternative="greater"`` (regression detection, canary monitoring):
  ``H0: mu_i <= m`` for all ``i``. Rejecting means "the candidate is worse than the
  tolerance ``m``" and triggers a rollback. Type-I error = a false rollback.
* ``alternative="less"`` (safety certification before promotion):
  ``H0: mu_i >= m``. Rejecting means "the candidate is better than ``m``" (for
  ``m = tolerance > 0`` this is non-inferiority). Type-I error = promoting a change
  that is in fact at least ``m`` worse. This is the error that costs money.

Derivation (test by betting)
----------------------------
Write ``s = +1`` for "greater" and ``s = -1`` for "less" and ``Y_i = s (X_i - m)``.
Then ``Y_i >= -(1 + s m)``. For any *predictable* bet ``lambda_i`` (a function of
``X_1..X_{i-1}`` only) with ``0 <= lambda_i <= b / (1 + s m)`` and ``b < 1``, the wealth

    K_t = prod_{i<=t} (1 + lambda_i Y_i)

is strictly positive (each factor is at least ``1 - b``), and under ``H0``

    E[1 + lambda_i Y_i | F_{i-1}] = 1 + lambda_i s (mu_i - m) <= 1,

because ``lambda_i >= 0`` and ``s (mu_i - m) <= 0``. So ``K`` is a non-negative
supermartingale with ``K_0 = 1``: an e-process. Ville's inequality (Ville 1939) gives

    P_H0( exists t : K_t >= 1/alpha ) <= alpha,

so rejecting the first time ``K_t >= 1/alpha`` is valid under continuous monitoring
and *any* stopping rule, including ones that look at the data. By optional stopping
for non-negative supermartingales, ``E[K_tau] <= 1`` for every stopping time ``tau``,
so a stopped e-process is an e-value (used by the stratified procedures).

Nothing above needs i.i.d. data, a variance bound or a CLT: only boundedness (the
declared cap) and the conditional-mean null. The bet only affects power.

Bets. ``"agrapa"`` is the approximate growth-rate adaptive bet of Waudby-Smith and
Ramdas (2023, "Estimating means of bounded random variables by betting", JRSS-B):
``lambda_i = clip(ybar / s2, 0, lambda_max)`` with running estimates of ``E[Y]`` and
``E[Y^2]`` from past data (the maximiser of the second-order expansion
``lambda E Y - lambda^2 E Y^2 / 2`` of ``E log(1 + lambda Y)``). ``"fixed"`` uses a
constant bet, which is what the planning module analyses in closed form.

Confidence sequence
-------------------
``BettingConfidenceSequence`` uses bets that do not depend on the candidate mean
``m``. With ``lambda_i in [0, b]`` and ``b < 1/2`` the lower wealth
``K+_t(m) = prod (1 + lambda_i (X_i - m))`` is positive for every ``m`` in [-1, 1] and
strictly decreasing in ``m``; the upper wealth ``K-_t(m) = prod (1 + lambda_i (m - X_i))``
is increasing. Each is a test martingale when ``m`` is the true mean, so by Ville and
a union bound over the two sides (``alpha/2`` each)

    C_t = { m : K+_t(m) < 2/alpha  and  K-_t(m) < 2/alpha } = [L_t, U_t]

satisfies ``P(forall t : mu in C_t) >= 1 - alpha`` (i.i.d. mean ``mu``). Monotonicity
makes ``C_t`` an exact interval found by bisection (no grid). Because coverage is
uniform in time, the running intersection of the ``C_t`` is also valid, which makes
reported intervals nested. The bets are the predictable plug-in choice of
Waudby-Smith and Ramdas (2023),
``lambda_t = min(b, sqrt(2 log(2/alpha) / (sigma2_{t-1} t log(1 + t))))``.
Using ``m``-independent bets (rather than their hedged-capital bets) is our
adaptation; it trades a little width for an interval that is exact and cheap.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

Alternative = Literal["greater", "less"]
Strategy = Literal["agrapa", "fixed"]

_EPS = 1e-12
_MAX_LOG = 700.0  # exp overflow guard


def normalise_differences(
    baseline: Sequence[float], candidate: Sequence[float], cap: float
) -> list[float]:
    """Return ``(candidate - baseline) / cap``; raise if any pair breaks the cap.

    The cap is a declared contract, not a tuning knob: clipping would silently void
    the guarantee, so a violation is an error.
    """
    if cap <= 0:
        raise ValueError("cost cap must be positive")
    if len(baseline) != len(candidate):
        raise ValueError("baseline and candidate must have the same length")
    out: list[float] = []
    for i, (b, c) in enumerate(zip(baseline, candidate, strict=True)):
        d = (c - b) / cap
        if not -1.0 - _EPS <= d <= 1.0 + _EPS:
            raise ValueError(f"pair {i}: |candidate - baseline| = {abs(c - b)} exceeds cap {cap}")
        out.append(max(-1.0, min(1.0, d)))
    return out


def _check_x(x: float) -> float:
    if not -1.0 - _EPS <= x <= 1.0 + _EPS or math.isnan(x):
        raise ValueError(f"observation {x} outside [-1, 1]; normalise by the cost cap")
    return max(-1.0, min(1.0, x))


def safe_exp(log_value: float) -> float:
    return math.exp(min(log_value, _MAX_LOG))


@dataclass
class BettingEProcess:
    """One-sided betting e-process for the mean of observations in [-1, 1].

    ``alternative="greater"`` gathers evidence that the mean exceeds ``null_mean``;
    ``"less"`` that it is below. ``max_bet`` is ``b`` in the module docstring (each
    wealth factor is at least ``1 - max_bet``).
    """

    null_mean: float
    alternative: Alternative
    strategy: Strategy = "agrapa"
    max_bet: float = 0.5
    fixed_bet: float | None = None
    prior_second_moment: float = 0.25

    n: int = field(default=0, init=False)
    log_wealth: float = field(default=0.0, init=False)
    max_log_wealth: float = field(default=0.0, init=False)
    _sum_y: float = field(default=0.0, init=False, repr=False)
    _sum_y2: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not -1.0 < self.null_mean < 1.0:
            raise ValueError("null_mean must lie strictly inside (-1, 1)")
        if self.alternative not in ("greater", "less"):
            raise ValueError("alternative must be 'greater' or 'less'")
        if not 0.0 < self.max_bet < 1.0:
            raise ValueError("max_bet must lie in (0, 1)")
        if self.strategy == "fixed":
            if self.fixed_bet is None or not 0.0 <= self.fixed_bet <= self.lambda_max:
                raise ValueError(f"fixed_bet must lie in [0, {self.lambda_max}]")
        elif self.strategy != "agrapa":
            raise ValueError("strategy must be 'agrapa' or 'fixed'")
        if self.prior_second_moment <= 0:
            raise ValueError("prior_second_moment must be positive")

    @property
    def sign(self) -> int:
        return 1 if self.alternative == "greater" else -1

    @property
    def lambda_max(self) -> float:
        return self.max_bet / (1.0 + self.sign * self.null_mean)

    def next_bet(self) -> float:
        """The predictable bet for the next observation (uses past data only)."""
        if self.strategy == "fixed":
            assert self.fixed_bet is not None
            return self.fixed_bet
        k = self.n + 1  # one pseudo-observation at y = 0 with E[Y^2] = prior
        ybar = self._sum_y / k
        s2 = (self.prior_second_moment + self._sum_y2) / k
        return min(max(ybar / s2, 0.0), self.lambda_max)

    def update(self, x: float) -> float:
        """Add one observation; return the current e-value."""
        x = _check_x(x)
        lam = self.next_bet()
        y = self.sign * (x - self.null_mean)
        self.log_wealth += math.log1p(lam * y)
        self.max_log_wealth = max(self.max_log_wealth, self.log_wealth)
        self.n += 1
        self._sum_y += y
        self._sum_y2 += y * y
        return self.e_value

    def extend(self, xs: Sequence[float]) -> float:
        for x in xs:
            self.update(x)
        return self.e_value

    @property
    def e_value(self) -> float:
        """Current wealth: an e-value at any stopping time."""
        return safe_exp(self.log_wealth)

    @property
    def max_e_value(self) -> float:
        return safe_exp(self.max_log_wealth)

    def rejected(self, alpha: float) -> bool:
        """True once the wealth has ever reached ``1/alpha`` (sticky, Ville-valid)."""
        _check_alpha(alpha)
        return self.max_log_wealth >= math.log(1.0 / alpha) - 1e-12


def e_process_path(
    xs: Sequence[float],
    null_mean: float,
    alternative: Alternative,
    *,
    strategy: Strategy = "agrapa",
    max_bet: float = 0.5,
    fixed_bet: float | None = None,
) -> list[float]:
    """Log-wealth after each observation (``log K_1, ..., log K_n``)."""
    ep = BettingEProcess(null_mean, alternative, strategy, max_bet, fixed_bet)
    out: list[float] = []
    for x in xs:
        ep.update(x)
        out.append(ep.log_wealth)
    return out


@dataclass(frozen=True)
class SequentialResult:
    """Outcome of monitoring a stream with optional stopping at the first crossing."""

    rejected: bool
    stopped_at: int  # number of observations used (first crossing, or all)
    e_value: float  # wealth at the stopping time
    max_e_value: float
    alpha: float


def sequential_test(
    xs: Sequence[float],
    null_mean: float,
    alternative: Alternative,
    alpha: float,
    *,
    strategy: Strategy = "agrapa",
    max_bet: float = 0.5,
) -> SequentialResult:
    """Monitor ``xs`` in order and stop at the first time ``K_t >= 1/alpha``."""
    _check_alpha(alpha)
    ep = BettingEProcess(null_mean, alternative, strategy, max_bet)
    thr = math.log(1.0 / alpha)
    for x in xs:
        ep.update(x)
        if ep.log_wealth >= thr:
            return SequentialResult(True, ep.n, ep.e_value, ep.max_e_value, alpha)
    return SequentialResult(False, ep.n, ep.e_value, ep.max_e_value, alpha)


def _check_alpha(alpha: float) -> None:
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie in (0, 1)")


@dataclass
class BettingConfidenceSequence:
    """Two-sided time-uniform confidence sequence for a mean in [-1, 1].

    ``interval()`` returns the current ``[L_t, U_t]``; ``nested_interval()`` the running
    intersection over all times at which an interval was requested.
    """

    alpha: float
    max_bet: float = 0.45
    prior_variance: float = 0.25
    bet_alpha: float | None = None  # level the bets are tuned for (default: alpha)

    _xs: list[float] = field(default_factory=list, init=False, repr=False)
    _lams: list[float] = field(default_factory=list, init=False, repr=False)
    _sum: float = field(default=0.0, init=False, repr=False)
    _sum_sq_dev: float = field(default=0.0, init=False, repr=False)
    _lo: float = field(default=-1.0, init=False, repr=False)
    _hi: float = field(default=1.0, init=False, repr=False)

    def __post_init__(self) -> None:
        _check_alpha(self.alpha)
        if self.bet_alpha is not None:
            _check_alpha(self.bet_alpha)
        if not 0.0 < self.max_bet < 0.5:
            raise ValueError("max_bet must lie in (0, 0.5) so wealth stays positive for all m")

    @property
    def n(self) -> int:
        return len(self._xs)

    def _next_bet(self) -> float:
        t = self.n + 1  # one pseudo-observation carries the prior variance
        sigma2 = (self.prior_variance + self._sum_sq_dev) / t
        a = self.alpha if self.bet_alpha is None else self.bet_alpha
        lam = math.sqrt(2.0 * math.log(2.0 / a) / (sigma2 * t * math.log1p(t)))
        return min(self.max_bet, lam)

    def update(self, x: float) -> None:
        x = _check_x(x)
        lam = self._next_bet()
        t = self.n + 1
        mu_prev = self._sum / t
        self._xs.append(x)
        self._lams.append(lam)
        self._sum += x
        self._sum_sq_dev += (x - mu_prev) ** 2

    def extend(self, xs: Sequence[float]) -> None:
        for x in xs:
            self.update(x)

    def _log_wealth(self, m: float, side: int) -> float:
        total = 0.0
        for x, lam in zip(self._xs, self._lams, strict=True):
            total += math.log1p(lam * side * (x - m))
        return total

    def interval(self) -> tuple[float, float]:
        thr = math.log(2.0 / self.alpha)
        lo = self._bound(thr, side=1)
        hi = self._bound(thr, side=-1)
        return lo, hi

    def nested_interval(self) -> tuple[float, float]:
        lo, hi = self.interval()
        self._lo = max(self._lo, lo)
        self._hi = min(self._hi, hi)
        if self._lo > self._hi:  # only on the probability-alpha miscoverage event
            mid = (self._lo + self._hi) / 2.0
            self._lo = self._hi = mid
        return self._lo, self._hi

    def _bound(self, thr: float, side: int) -> float:
        """Lower (side=+1) or upper (side=-1) endpoint, rounded outwards."""
        # side=+1: wealth decreasing in m; rejected set is [-1, L). side=-1: mirror.
        inner, outer = (1.0, -1.0) if side == 1 else (-1.0, 1.0)
        if self._log_wealth(outer, side) < thr:
            return outer  # nothing rejected on this side
        if self._log_wealth(inner, side) >= thr:
            return inner  # degenerate: everything rejected
        rej, keep = outer, inner
        for _ in range(60):
            mid = (rej + keep) / 2.0
            if self._log_wealth(mid, side) >= thr:
                rej = mid
            else:
                keep = mid
        return rej  # the last rejected point: conservative (outwards)
