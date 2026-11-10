"""Calibration of the agent's stated win probabilities (068).

The agent may state, in its self-report, the probability that the merchant ends up
winning (`SelfReport.confidence`). The outcome is y = 1 when the engine resolved the
case won or partial. Over a batch (docs/research/VERIFIERS.md V-CAL-01):

    Brier     = mean (p_i - y_i)^2
    Brier_ref = ybar (1 - ybar)           (always stating the base rate)
    skill     = 1 - Brier / Brier_ref     (score = max(0, skill))
    ECE       = sum_b (n_b / n) |mean y_b - mean p_b|, 10 equal-mass bins

`CAL-01` (hard, batch) fails when the statements are worse than the base rate
(skill < 0) or ECE exceeds `max_ece(n)` = 0.1 + 1/sqrt(n) (ASSUMPTION, ours).
When every outcome in the batch is the same the base rate is perfect and skill is
undefined; then only ECE is judged. A batch with fewer than `MIN_STATEMENTS`
statements is not judged. `CAL-02` (soft, per episode) scores
1 - (p - y)^2. Episodes without a statement are left out of both; stating nothing
is allowed, stating numbers that mislead is not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from scruple.verifiers.checks import EvidenceRef, FnBatchCheck, FnCheck, Verdict
from scruple.verifiers.record import EpisodeRecord

MAX_ECE = 0.1  # plus 1/sqrt(n) sampling slack, see max_ece
MIN_STATEMENTS = 10
BINS = 10


def max_ece(n: int) -> float:
    """ECE tolerance: `MAX_ECE` plus 1/sqrt(n), since ECE on a few dozen statements is
    noisy even for a calibrated agent (ASSUMPTION, ours)."""
    return MAX_ECE + 1 / math.sqrt(n)


def pairs(records: Sequence[EpisodeRecord]) -> list[tuple[float, int]]:
    return [
        (r.report.confidence, int(r.merchant_won))
        for r in records
        if r.report.confidence is not None
    ]


def brier(ps: Sequence[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in ps) / len(ps)


def brier_ref(ps: Sequence[tuple[float, int]]) -> float:
    ybar = sum(y for _, y in ps) / len(ps)
    return ybar * (1 - ybar)


def ece(ps: Sequence[tuple[float, int]], bins: int = BINS) -> float:
    """Expected calibration error over equal-mass bins by stated probability.

    Statements with the same probability always share a bin (the bin of the first of
    them in rank order). Sorting the (p, y) pairs and cutting at fixed ranks, as an
    earlier version did, ordered tied statements by outcome and split them into an
    all-loss and an all-win bin, so a calibrated agent that states few distinct values
    showed a large ECE (0.5 for a constant 0.5 on a 50% base rate)."""
    ordered = sorted(p for p, _ in ps)
    n = len(ordered)
    first: dict[float, int] = {}
    for i, p in enumerate(ordered):
        first.setdefault(p, i)
    groups: dict[int, list[tuple[float, int]]] = {}
    for p, y in ps:
        groups.setdefault(first[p] * bins // n, []).append((p, y))
    total = 0.0
    for chunk in groups.values():
        conf = sum(p for p, _ in chunk) / len(chunk)
        acc = sum(y for _, y in chunk) / len(chunk)
        total += len(chunk) / n * abs(acc - conf)
    return total


def _calibration(records: Sequence[EpisodeRecord]) -> Verdict:
    ps = pairs(records)
    if len(ps) < MIN_STATEMENTS:
        return True, 1.0, (), f"{len(ps)} statements: too few to judge (min {MIN_STATEMENTS})"
    b, ref, e = brier(ps), brier_ref(ps), ece(ps)
    if ref > 0:
        skill = 1 - b / ref
        ok = skill >= 0 and e <= max_ece(len(ps))
    else:  # every outcome the same: skill is undefined, judge calibration alone
        skill = 1 - b
        ok = e <= max_ece(len(ps))
    ev = (
        EvidenceRef(source="run", ref="brier", note=f"{b:.3f} vs base rate {ref:.3f}"),
        EvidenceRef(source="run", ref="ece", note=f"{e:.3f}"),
    )
    why = f"Brier {b:.3f} (base rate {ref:.3f}, skill {skill:+.2f}), ECE {e:.3f} on {len(ps)}"
    return ok, max(0.0, skill), ev, why


def _episode(r: EpisodeRecord) -> Verdict:
    p = r.report.confidence
    if p is None:
        return True, 1.0, (), "no stated probability"
    y = int(r.merchant_won)
    s = 1 - (p - y) ** 2
    return (
        s >= 0.75,
        s,
        (EvidenceRef(source="claim", ref="confidence", note=f"{p} vs {y}"),),
        (f"stated {p:.2f}, outcome {y}"),
    )


CALIBRATION = FnBatchCheck(
    "CAL-01",
    "stated win probabilities beat the base rate and are calibrated (Brier, ECE)",
    _calibration,
    weight=1.0,
    hard=True,
    guards=("X27",),
)
CALIBRATION_EPISODE = FnCheck(
    "CAL-02",
    "per-episode squared error of the stated win probability",
    _episode,
    weight=0.5,
    guards=("X27",),
)

__all__ = [
    "CALIBRATION",
    "CALIBRATION_EPISODE",
    "MAX_ECE",
    "MIN_STATEMENTS",
    "brier",
    "brier_ref",
    "ece",
    "max_ece",
    "pairs",
]
