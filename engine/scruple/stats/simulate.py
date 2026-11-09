"""Seeded synthetic distributions and Monte Carlo helpers for the stats package.

Every number these produce is synthetic. The distributions mimic the shape of paired
loss differences in the dispute gate: most cases unchanged (difference 0), a few
better, a few worse, occasionally by a lot.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import NormalDist

from .sequential import Alternative, BettingEProcess


@dataclass(frozen=True)
class Ternary:
    """``X = +worse`` w.p. ``p_worse``, ``-better`` w.p. ``p_better``, else 0."""

    p_worse: float
    worse: float
    p_better: float
    better: float

    def __post_init__(self) -> None:
        if min(self.p_worse, self.p_better) < 0 or self.p_worse + self.p_better > 1:
            raise ValueError("invalid probabilities")
        if not (0 <= self.worse <= 1 and 0 <= self.better <= 1):
            raise ValueError("magnitudes must lie in [0, 1]")

    @property
    def mean(self) -> float:
        return self.p_worse * self.worse - self.p_better * self.better

    @property
    def variance(self) -> float:
        m2 = self.p_worse * self.worse**2 + self.p_better * self.better**2
        return m2 - self.mean**2

    def sample(self, rng: random.Random) -> float:
        u = rng.random()
        if u < self.p_worse:
            return self.worse
        if u < self.p_worse + self.p_better:
            return -self.better
        return 0.0


@dataclass(frozen=True)
class ScaledBeta:
    """``X = 2 B - 1`` with ``B ~ Beta(a, b)``: continuous, mean ``(a - b)/(a + b)``."""

    a: float
    b: float

    @property
    def mean(self) -> float:
        return (self.a - self.b) / (self.a + self.b)

    def sample(self, rng: random.Random) -> float:
        return 2.0 * rng.betavariate(self.a, self.b) - 1.0


@dataclass(frozen=True)
class RareShock:
    """Rare large loss: ``X = 1`` w.p. ``p``, else ``v`` (chosen to hit ``mean``)."""

    p: float
    target_mean: float

    def __post_init__(self) -> None:
        if not 0 < self.p < 1:
            raise ValueError("p must lie in (0, 1)")
        if not -1 <= self.low_value <= 1:
            raise ValueError("target mean not reachable with this p")

    @property
    def low_value(self) -> float:
        return (self.target_mean - self.p) / (1.0 - self.p)

    @property
    def mean(self) -> float:
        return self.target_mean

    def sample(self, rng: random.Random) -> float:
        return 1.0 if rng.random() < self.p else self.low_value


Sampler = Ternary | ScaledBeta | RareShock


def ternary_with_mean(mean: float, p_changed: float, magnitude: float) -> Ternary:
    """A ternary law with ``P(X != 0) = p_changed``, ``|X| = magnitude`` and a given mean."""
    if p_changed <= 0 or magnitude <= 0:
        raise ValueError("p_changed and magnitude must be positive")
    diff = mean / magnitude  # p_worse - p_better
    p_w = (p_changed + diff) / 2.0
    p_b = (p_changed - diff) / 2.0
    if p_w < -1e-12 or p_b < -1e-12:
        raise ValueError("mean not reachable with this p_changed and magnitude")
    return Ternary(max(p_w, 0.0), magnitude, max(p_b, 0.0), magnitude)


def draw(dist: Sampler, n: int, rng: random.Random) -> list[float]:
    return [dist.sample(rng) for _ in range(n)]


# --- stopping rules --------------------------------------------------------------------
# A stopping rule sees (t, log_wealth, running mean of X) after observation t and
# returns True to stop. It may use anything observed so far: optional stopping.

StoppingRule = Callable[[int, float, float], bool]


def stopping_rules(alpha: float, horizon: int, seed: int) -> dict[str, StoppingRule]:
    """A battery of data-dependent and data-independent stopping rules."""
    thr = math.log(1.0 / alpha)
    rng = random.Random(seed)
    stop_t = min(horizon, 1 + int(rng.expovariate(1.0 / max(horizon / 3, 1))))

    def fixed(t: int, lw: float, mean: float) -> bool:
        return t >= horizon

    def first_crossing(t: int, lw: float, mean: float) -> bool:
        return lw >= thr or t >= horizon

    def daily_peek(t: int, lw: float, mean: float) -> bool:
        return (t % 50 == 0 and lw >= thr) or t >= horizon

    def looks_bad(t: int, lw: float, mean: float) -> bool:
        # "Stop when the running mean looks like a regression": the classic p-hacker.
        return (t >= 20 and mean > 0.0 and lw >= 0.5 * thr) or lw >= thr or t >= horizon

    def give_up(t: int, lw: float, mean: float) -> bool:
        return lw <= math.log(0.5) or lw >= thr or t >= horizon

    def random_time(t: int, lw: float, mean: float) -> bool:
        return t >= stop_t

    return {
        "fixed_horizon": fixed,
        "first_crossing": first_crossing,
        "peek_every_50": daily_peek,
        "stop_when_looks_bad": looks_bad,
        "give_up_below_half": give_up,
        "random_time": random_time,
    }


def stopped_rejections(
    xs: Sequence[float],
    null_mean: float,
    alternative: Alternative,
    alpha: float,
    rules: dict[str, StoppingRule],
) -> dict[str, bool]:
    """For one path, whether each stopping rule ends with ``K_tau >= 1/alpha``."""
    ep = BettingEProcess(null_mean, alternative)
    thr = math.log(1.0 / alpha)
    pending = dict(rules)
    out: dict[str, bool] = {}
    total = 0.0
    for t, x in enumerate(xs, start=1):
        ep.update(x)
        total += x
        mean = total / t
        for name in list(pending):
            if pending[name](t, ep.log_wealth, mean):
                out[name] = ep.log_wealth >= thr
                del pending[name]
        if not pending:
            break
    for name in pending:
        out[name] = ep.log_wealth >= thr
    out["ever_crossed"] = ep.max_log_wealth >= thr
    return out


# --- naive baselines (for contrast only; NOT anytime-valid) ------------------------------


def z_statistic(xs: Sequence[float], null_mean: float) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 0.0 if mean == null_mean else math.copysign(math.inf, mean - null_mean)
    return (mean - null_mean) / math.sqrt(var / n)


def peeking_z_rejects(
    xs: Sequence[float],
    null_mean: float,
    alpha: float,
    every: int,
    min_n: int = 20,
) -> bool:
    """One-sided z-test (``greater``) re-run every ``every`` observations; reject if ever.

    Valid at a single fixed n; repeated looks inflate type-I error. Running sums keep
    it linear-time.
    """
    z_crit = NormalDist().inv_cdf(1.0 - alpha)
    s = s2 = 0.0
    for t, x in enumerate(xs, start=1):
        s += x
        s2 += x * x
        if t >= min_n and t % every == 0:
            mean = s / t
            var = (s2 - t * mean * mean) / (t - 1)
            if var > 0 and (mean - null_mean) / math.sqrt(var / t) > z_crit:
                return True
    return False


def binomial_se(p: float, n: int) -> float:
    return math.sqrt(max(p * (1 - p), 1e-12) / n)
