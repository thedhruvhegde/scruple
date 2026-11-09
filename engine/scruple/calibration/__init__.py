"""Calibrate scruple's history simulator from customer aggregates only (roadmap 153).

Rows go into `compute_aggregates` and nothing else; it returns suppressed aggregates
(every count at least `k`, default 10). `fit_profile` turns aggregates into a
`CalibrationProfile`; `calibrate` does both and reports the divergence of the default
and the fitted generator from the history. See docs/BACKTEST.md.
"""

from scruple.calibration.aggregates import (
    OTHER,
    CalibrationConfig,
    CountTable,
    EvidenceRates,
    HistoryAggregates,
    InsufficientHistoryError,
    Rate,
    compute_aggregates,
    suppress_counts,
    suppress_rate,
)
from scruple.calibration.calibrate import CalibrationReport, calibrate, compare
from scruple.calibration.profile import (
    DEFAULT_PROFILE,
    CalibrationProfile,
    Divergence,
    ReasonProfile,
    divergence,
    fit_profile,
    to_case_mix,
)
from scruple.calibration.synthetic import (
    SYNTHETIC_LABEL,
    SyntheticHistory,
    generate_history,
    stripe_disputes,
)

__all__ = [
    "DEFAULT_PROFILE",
    "OTHER",
    "SYNTHETIC_LABEL",
    "CalibrationConfig",
    "CalibrationProfile",
    "CalibrationReport",
    "CountTable",
    "Divergence",
    "EvidenceRates",
    "HistoryAggregates",
    "InsufficientHistoryError",
    "Rate",
    "ReasonProfile",
    "SyntheticHistory",
    "calibrate",
    "compare",
    "compute_aggregates",
    "divergence",
    "fit_profile",
    "generate_history",
    "stripe_disputes",
    "suppress_counts",
    "suppress_rate",
    "to_case_mix",
]
