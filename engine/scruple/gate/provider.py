"""Pluggable statistics for the gate, the canary controller and the scorecard.

The gate decides on intervals, never on raw means alone. Which interval is a policy
choice that will change (anytime-valid sequential tests are being written
separately), so callers depend on the `StatsProvider` protocol, not on a function.
`BootstrapProvider` wraps the existing seeded bootstrap and is the default.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from scruple.gate.stats import paired_bootstrap_ci


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


def default_provider(resamples: int = 2_000, seed: int = 7) -> StatsProvider:
    return BootstrapProvider(resamples, seed)
