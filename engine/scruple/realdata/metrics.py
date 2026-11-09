"""Classification and probability metrics with percentile-bootstrap CIs.

Pure Python, no numpy. Definitions (all checked against hand-computed values and,
where installed, scikit-learn in tests/test_realdata_metrics.py):

- accuracy: share of exact matches.
- macro-F1: unweighted mean of per-class F1 over the classes present in the gold
  labels (sklearn `f1_score(labels=sorted(set(y_true)), average="macro")`). A class
  with no true positives and no predictions has F1 0. Predictions outside the gold
  set (including the invalid marker) count against recall, never as a class.
- recall per class: TP / gold count.
- AUROC: Mann-Whitney U with average ranks for ties; None with one class only.
- Brier: mean squared error of the probability.
- ECE: `bins` equal-width bins on [0, 1] (1.0 in the last), sum over bins of
  (bin size / n) * |mean(y) - mean(p)|.
- balanced accuracy: mean of the recall of each gold class.
- bootstrap: resample instances with replacement (`random.Random(seed)`), recompute
  the metric, take the alpha/2 and 1-alpha/2 percentiles (linear interpolation).
  Replicates where the metric is undefined are skipped and counted.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Hashable, Sequence

from pydantic import BaseModel, ConfigDict


class Estimate(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: float | None
    lo: float | None
    hi: float | None
    n: int
    n_boot: int
    n_boot_undefined: int = 0


def _check(y: Sequence[object], p: Sequence[object]) -> None:
    if len(y) != len(p):
        raise ValueError(f"length mismatch: {len(y)} gold vs {len(p)} predictions")


def accuracy[T: Hashable](y: Sequence[T], p: Sequence[T]) -> float | None:
    _check(y, p)
    if not y:
        return None
    return sum(a == b for a, b in zip(y, p, strict=True)) / len(y)


def confusion[T: Hashable](y: Sequence[T], p: Sequence[T]) -> dict[T, dict[T, int]]:
    """gold -> predicted -> count (only non-zero cells)."""
    _check(y, p)
    out: dict[T, dict[T, int]] = {}
    for a, b in zip(y, p, strict=True):
        row = out.setdefault(a, {})
        row[b] = row.get(b, 0) + 1
    return out


def per_class_recall[T: Hashable](y: Sequence[T], p: Sequence[T]) -> dict[T, float]:
    _check(y, p)
    gold = Counter(y)
    tp = Counter(a for a, b in zip(y, p, strict=True) if a == b)
    return {c: tp[c] / n for c, n in gold.items()}


def per_class_f1[T: Hashable](y: Sequence[T], p: Sequence[T]) -> dict[T, float]:
    _check(y, p)
    gold = Counter(y)
    pred = Counter(p)
    tp = Counter(a for a, b in zip(y, p, strict=True) if a == b)
    out: dict[T, float] = {}
    for c in gold:
        denom = gold[c] + pred[c]
        out[c] = 2 * tp[c] / denom if denom else 0.0
    return out


def macro_f1[T: Hashable](y: Sequence[T], p: Sequence[T]) -> float | None:
    if not y:
        return None
    f = per_class_f1(y, p)
    return sum(f.values()) / len(f)


def balanced_accuracy[T: Hashable](y: Sequence[T], p: Sequence[T]) -> float | None:
    if not y:
        return None
    r = per_class_recall(y, p)
    return sum(r.values()) / len(r)


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def auroc(y: Sequence[int], s: Sequence[float]) -> float | None:
    _check(y, s)
    n_pos = sum(1 for v in y if v == 1)
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    r = _ranks(s)
    rank_pos = sum(r[i] for i, v in enumerate(y) if v == 1)
    return (rank_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def brier(y: Sequence[int], s: Sequence[float]) -> float | None:
    _check(y, s)
    if not y:
        return None
    return sum((p - v) ** 2 for v, p in zip(y, s, strict=True)) / len(y)


def ece(y: Sequence[int], s: Sequence[float], bins: int = 10) -> float | None:
    _check(y, s)
    if not y:
        return None
    if bins < 1:
        raise ValueError("bins must be >= 1")
    count = [0] * bins
    sum_y = [0.0] * bins
    sum_p = [0.0] * bins
    for v, p in zip(y, s, strict=True):
        b = min(int(p * bins), bins - 1)
        count[b] += 1
        sum_y[b] += v
        sum_p[b] += p
    n = len(y)
    return sum(abs(sum_y[b] - sum_p[b]) / n for b in range(bins) if count[b])


def reliability(y: Sequence[int], s: Sequence[float], bins: int = 10) -> list[dict[str, float]]:
    """Per-bin (lo, hi, n, mean p, observed rate) for a calibration table."""
    _check(y, s)
    rows: list[dict[str, float]] = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(s) if min(int(p * bins), bins - 1) == b]
        if idx:
            rows.append(
                {
                    "lo": lo,
                    "hi": hi,
                    "n": float(len(idx)),
                    "mean_p": sum(s[i] for i in idx) / len(idx),
                    "rate": sum(y[i] for i in idx) / len(idx),
                }
            )
    return rows


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (numpy's default), q in [0, 1]."""
    if not sorted_values:
        raise ValueError("empty")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def bootstrap(
    metric: Callable[[Sequence[int]], float | None],
    n: int,
    *,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> Estimate:
    """`metric(indices)` evaluates the statistic on a list of instance indices."""
    point = metric(list(range(n))) if n else None
    if n == 0 or point is None or n_boot == 0:
        return Estimate(value=point, lo=None, hi=None, n=n, n_boot=0)
    rng = random.Random(seed)
    reps: list[float] = []
    undefined = 0
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        v = metric(idx)
        if v is None or math.isnan(v):
            undefined += 1
        else:
            reps.append(v)
    if not reps:
        return Estimate(value=point, lo=None, hi=None, n=n, n_boot=0, n_boot_undefined=undefined)
    reps.sort()
    return Estimate(
        value=point,
        lo=percentile(reps, alpha / 2),
        hi=percentile(reps, 1 - alpha / 2),
        n=n,
        n_boot=len(reps),
        n_boot_undefined=undefined,
    )


def labels_ci[T: Hashable](
    fn: Callable[[Sequence[T], Sequence[T]], float | None],
    y: Sequence[T],
    p: Sequence[T],
    *,
    n_boot: int = 1000,
    seed: int = 0,
) -> Estimate:
    _check(y, p)
    return bootstrap(
        lambda idx: fn([y[i] for i in idx], [p[i] for i in idx]), len(y), n_boot=n_boot, seed=seed
    )


def scores_ci(
    fn: Callable[[Sequence[int], Sequence[float]], float | None],
    y: Sequence[int],
    s: Sequence[float],
    *,
    n_boot: int = 1000,
    seed: int = 0,
) -> Estimate:
    _check(y, s)
    return bootstrap(
        lambda idx: fn([y[i] for i in idx], [s[i] for i in idx]), len(y), n_boot=n_boot, seed=seed
    )
