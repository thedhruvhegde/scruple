"""Fit a profile to a history's aggregates and report the divergence before and after.

`calibrate` is the whole 153 step: aggregate (rows in, suppressed aggregates out), fit
(aggregates in, profile out), then compare. The comparison draws a SYNTHETIC history
from the default profile ("before") and from the fitted one ("after"), aggregates each
with the same suppression, and measures each against the customer aggregates. Only
aggregates are compared; the customer rows are not used after the first step.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from scruple.calibration.aggregates import (
    CalibrationConfig,
    HistoryAggregates,
    compute_aggregates,
)
from scruple.calibration.profile import (
    DEFAULT_PROFILE,
    CalibrationProfile,
    Divergence,
    divergence,
    fit_profile,
)
from scruple.calibration.synthetic import SYNTHETIC_LABEL, generate_history
from scruple.ingest.models import ExternalCase, LedgerMovement


class CalibrationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    aggregates: HistoryAggregates
    profile: CalibrationProfile
    before: Divergence  # default profile vs the history
    after: Divergence  # fitted profile vs the history
    n_synthetic: int
    seed: int
    comparison_label: str = SYNTHETIC_LABEL

    @property
    def improved(self) -> bool:
        return self.after.overall <= self.before.overall


def synthetic_aggregates(
    profile: CalibrationProfile, n: int, seed: int, config: CalibrationConfig
) -> HistoryAggregates:
    h = generate_history(profile, n, seed)
    return compute_aggregates(h.cases, h.movements, config)


def compare(
    aggs: HistoryAggregates,
    profile: CalibrationProfile,
    *,
    prior: CalibrationProfile = DEFAULT_PROFILE,
    n_synthetic: int = 5_000,
    seed: int = 0,
) -> CalibrationReport:
    """Divergence of `prior` and of `profile` from `aggs` (aggregates only)."""
    cfg = CalibrationConfig(k=aggs.k, amount_edges=aggs.amount_edges)
    before = divergence(aggs, synthetic_aggregates(prior, n_synthetic, seed, cfg))
    after = divergence(aggs, synthetic_aggregates(profile, n_synthetic, seed, cfg))
    return CalibrationReport(
        aggregates=aggs,
        profile=profile,
        before=before,
        after=after,
        n_synthetic=n_synthetic,
        seed=seed,
    )


def calibrate(
    cases: Iterable[ExternalCase],
    movements: Iterable[LedgerMovement] = (),
    config: CalibrationConfig | None = None,
    *,
    prior: CalibrationProfile = DEFAULT_PROFILE,
    n_synthetic: int = 5_000,
    seed: int = 0,
) -> CalibrationReport:
    aggs = compute_aggregates(cases, movements, config)
    return compare(aggs, fit_profile(aggs, prior), prior=prior, n_synthetic=n_synthetic, seed=seed)
