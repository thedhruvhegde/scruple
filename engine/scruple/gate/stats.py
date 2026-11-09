"""Paired statistics. Deterministic: all resampling uses an explicit seed."""

from __future__ import annotations

import random
from collections.abc import Sequence


def paired_bootstrap_ci(
    diffs: Sequence[int], resamples: int, confidence: float, seed: int
) -> tuple[float, float, float]:
    """Mean of paired differences with a percentile bootstrap CI.

    Most paired differences are exactly zero (cases the rule does not touch), so we
    sample the number of non-zero draws from a Binomial and then draw only those.
    This is distributionally identical to the naive bootstrap and much faster.
    """
    n = len(diffs)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = sum(diffs) / n
    nonzero = [d for d in diffs if d != 0]
    if not nonzero:
        return 0.0, 0.0, 0.0
    p = len(nonzero) / n
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(resamples):
        k = rng.binomialvariate(n, p)
        means.append(sum(rng.choices(nonzero, k=k)) / n if k else 0.0)
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    lo = means[int(alpha * resamples)]
    hi = means[min(resamples - 1, int((1.0 - alpha) * resamples))]
    return mean, lo, hi
