"""Pluggable statistics for the gate, the canary controller and the scorecard.

The gate decides on intervals, never on raw means alone. Which interval is a policy
choice that will change (anytime-valid sequential tests are being written
separately), so callers depend on the `StatsProvider` protocol, not on a function.
`BootstrapProvider` wraps the existing seeded bootstrap and is the default.
`EValueStatsProvider` gives anytime-valid intervals from betting confidence
sequences (scruple.stats); `provider_for` picks one from `GateConfig.stats`.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from scruple.gate.stats import paired_bootstrap_ci
from scruple.stats import BettingConfidenceSequence, LossPair, StatsGateConfig, evaluate_pairs

if TYPE_CHECKING:
    from scruple.gate.config import GateConfig


class Interval(BaseModel):
    """A point estimate with a two-sided interval at `confidence`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mean: float
    low: float
    high: float
    confidence: float
    n: int
    method: str


@runtime_checkable
class StatsProvider(Protocol):
    """What the gate needs from a statistics engine. Must be deterministic."""

    @property
    def name(self) -> str: ...

    def paired_interval(self, diffs: Sequence[int], confidence: float) -> Interval:
        """Interval for the mean of paired differences (same cases, two policies)."""
        ...

    def two_sample_interval(
        self, treatment: Sequence[float], control: Sequence[float], confidence: float
    ) -> Interval:
        """Interval for mean(treatment) - mean(control), independent groups."""
        ...


class BootstrapProvider:
    """Seeded percentile bootstrap. Paired case delegates to `paired_bootstrap_ci`
    unchanged, so shadow decisions are identical with or without the provider."""

    def __init__(self, resamples: int = 2_000, seed: int = 7) -> None:
        if resamples < 1:
            raise ValueError("resamples must be positive")
        self.resamples = resamples
        self.seed = seed

    @property
    def name(self) -> str:
        return "percentile-bootstrap"

    def paired_interval(self, diffs: Sequence[int], confidence: float) -> Interval:
        mean, lo, hi = paired_bootstrap_ci(diffs, self.resamples, confidence, self.seed)
        return Interval(
            mean=mean,
            low=lo,
            high=hi,
            confidence=confidence,
            n=len(diffs),
            method=f"{self.name}-paired",
        )

    def two_sample_interval(
        self, treatment: Sequence[float], control: Sequence[float], confidence: float
    ) -> Interval:
        n = len(treatment) + len(control)
        if not treatment or not control:
            return Interval(
                mean=0.0,
                low=0.0,
                high=0.0,
                confidence=confidence,
                n=n,
                method=f"{self.name}-two-sample",
            )
        mean = sum(treatment) / len(treatment) - sum(control) / len(control)
        rng = random.Random(self.seed)
        stats: list[float] = []
        for _ in range(self.resamples):
            t = rng.choices(treatment, k=len(treatment))
            c = rng.choices(control, k=len(control))
            stats.append(sum(t) / len(t) - sum(c) / len(c))
        stats.sort()
        alpha = (1.0 - confidence) / 2.0
        lo = stats[int(alpha * self.resamples)]
        hi = stats[min(self.resamples - 1, int((1.0 - alpha) * self.resamples))]
        return Interval(
            mean=mean,
            low=lo,
            high=hi,
            confidence=confidence,
            n=n,
            method=f"{self.name}-two-sample",
        )


class EValueStatsProvider:
    """Anytime-valid intervals from betting confidence sequences.

    ``cost_cap`` is a declared bound on every observation's absolute value (USD
    minor), fixed before outcomes are seen. An observation above it raises
    ``ValueError``: clipping would silently void the guarantee.

    Paired: ``scruple.stats.evaluate_pairs`` on one case type, whose pooled
    time-uniform confidence sequence at ``alpha = 1 - confidence`` is returned. It
    covers the mean at every sample size simultaneously, so it stays valid however
    often it is looked at (docs/math/01_anytime_paired_test.md).

    Two-sample: one confidence sequence per group at ``alpha / 2`` each; the
    difference interval is ``[L_t - U_c, U_t - L_c]``, valid by a union bound for
    independent i.i.d. groups. It is wider than a bootstrap interval, by design.
    """

    def __init__(self, cost_cap: float) -> None:
        if not cost_cap > 0:
            raise ValueError("cost_cap must be positive")
        self.cost_cap = float(cost_cap)

    @property
    def name(self) -> str:
        return "betting-cs"

    def paired_interval(self, diffs: Sequence[int], confidence: float) -> Interval:
        method = f"{self.name}-paired"
        if not diffs:
            return Interval(mean=0.0, low=0.0, high=0.0, confidence=confidence, n=0, method=method)
        pairs = [
            LossPair(case_id=str(i), case_type="all", baseline_loss=0.0, candidate_loss=float(d))
            for i, d in enumerate(diffs)
        ]
        v = evaluate_pairs(
            pairs, StatsGateConfig(cost_cap=self.cost_cap, alpha_certify=1.0 - confidence)
        )
        return Interval(
            mean=v.mean_diff,
            low=v.ci_low,
            high=v.ci_high,
            confidence=confidence,
            n=len(diffs),
            method=method,
        )

    def _sequence(self, xs: Sequence[float], alpha: float) -> tuple[float, float]:
        cs = BettingConfidenceSequence(alpha)
        for x in xs:
            if abs(x) > self.cost_cap:
                raise ValueError(f"|{x}| exceeds the declared cost cap {self.cost_cap}")
            cs.update(x / self.cost_cap)
        lo, hi = cs.interval()
        return lo * self.cost_cap, hi * self.cost_cap

    def two_sample_interval(
        self, treatment: Sequence[float], control: Sequence[float], confidence: float
    ) -> Interval:
        n = len(treatment) + len(control)
        method = f"{self.name}-two-sample"
        if not treatment or not control:
            return Interval(mean=0.0, low=0.0, high=0.0, confidence=confidence, n=n, method=method)
        alpha = (1.0 - confidence) / 2.0
        t_lo, t_hi = self._sequence(treatment, alpha)
        c_lo, c_hi = self._sequence(control, alpha)
        mean = sum(treatment) / len(treatment) - sum(control) / len(control)
        return Interval(
            mean=mean,
            low=t_lo - c_hi,
            high=t_hi - c_lo,
            confidence=confidence,
            n=n,
            method=method,
        )


def default_provider(resamples: int = 2_000, seed: int = 7) -> StatsProvider:
    return BootstrapProvider(resamples, seed)


def provider_for(cfg: GateConfig) -> StatsProvider:
    """The provider `GateConfig.stats` selects ("bootstrap" by default)."""
    if cfg.stats == "evalue":
        return EValueStatsProvider(cfg.evalue_cost_cap_usd_minor)
    return BootstrapProvider(cfg.bootstrap_resamples, cfg.stats_seed)
