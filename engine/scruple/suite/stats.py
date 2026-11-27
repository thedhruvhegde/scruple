"""Small, deterministic interval estimates for suite and env-eval reports.

- `wilson`: the Wilson score interval for a pass rate (good at small n and at 0/n).
- `boot_mean`: percentile bootstrap of a mean (2,000 resamples, `random.Random(0)`).
- `paired_diff`: percentile bootstrap of the mean paired difference `a - b`, the same
  resampling scheme as `scruple.eval.scoring.paired_cost` (pairs resampled together).
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

N_BOOT = 2000


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(q * len(xs))))
    return xs[i]


def boot_mean(
    xs: Sequence[float], *, n_boot: int = N_BOOT, seed: int = 0
) -> tuple[float, float, float]:
    """(mean, lo, hi)."""
    if not xs:
        return (0.0, 0.0, 0.0)
    m = sum(xs) / len(xs)
    if len(xs) == 1:
        return (m, m, m)
    rng = random.Random(seed)
    n = len(xs)
    means = [sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot)]
    return (round(m, 6), round(_pct(means, 0.025), 6), round(_pct(means, 0.975), 6))


def paired_diff(
    a: Sequence[float], b: Sequence[float], *, n_boot: int = N_BOOT, seed: int = 0
) -> tuple[float, float, float]:
    """Mean of a - b over pairs, with a 95% percentile bootstrap CI."""
    if len(a) != len(b):
        raise ValueError("paired samples must have the same length")
    return boot_mean([x - y for x, y in zip(a, b, strict=True)], n_boot=n_boot, seed=seed)


__all__ = ["N_BOOT", "boot_mean", "paired_diff", "wilson"]
