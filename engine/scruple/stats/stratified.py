"""Per-case-type (stratum) regression guarantees from e-values.

Setting
-------
Cases arrive one at a time, each with a type ``k in {1..K}`` (a stratum, e.g. a
dispute scenario). Stratum ``k`` has frequency ``pi_k`` (possibly 1% or less) and
its own normalised paired difference stream. For each stratum the null is
``H0_k: E[X | past, type = k] <= m_k`` ("type k did not regress beyond its
tolerance"), tested by a betting e-process ``E_k`` that only moves when a type-k case
arrives (see ``sequential.py``). Under ``H0_k`` each ``E_k`` is a non-negative
supermartingale with respect to the *global* filtration, because its factor for a
type-k case has conditional expectation at most 1 and its factor for any other case
is exactly 1. So ``E_{k,tau}`` is an e-value for every global stopping time ``tau``.

Three multiplicity procedures, all valid under *arbitrary* dependence between strata:

1. ``e_bonferroni`` / ``StratifiedMonitor.fwer_rejections`` (FWER, time-uniform).
   Weights ``w_k >= 0`` with ``sum w_k = K``. Reject ``H0_k`` once
   ``sup_t E_{k,t} >= K / (alpha w_k)``. By Ville for each true null and a union bound,
   ``P(any true null ever rejected) <= sum_{k null} alpha w_k / K <= alpha``.
   Weights let expensive or rare strata get more of the error budget.

2. ``e_closed_mean`` (FWER, at a stopping time; unweighted). Closed testing (Marcus,
   Peritz and Gabriel 1976) with the arithmetic-mean intersection e-value
   ``E_S = mean_{k in S} E_k``, which is an e-value under ``H0_S`` by linearity (Vovk
   and Wang 2021, "E-values: calibration, combination and applications", Ann. Statist.,
   study this merging function). Reject ``H0_k`` iff ``E_S >= 1/alpha`` for every
   ``S`` containing ``k``. If ``I0`` is the set of true nulls, any false rejection
   implies ``E_{I0} >= 1/alpha``, which has probability at most ``alpha`` (Markov). The
   minimum of ``E_S`` over ``S`` of size ``s`` containing ``k`` is ``e_k`` plus the
   ``s - 1`` smallest other e-values, divided by ``s``; checking all ``s`` gives an
   exact ``O(K log K)`` per-stratum shortcut. It dominates unweighted e-Bonferroni
   (if ``e_k >= K/alpha`` then every ``E_S >= e_k/|S| >= 1/alpha``).

3. ``e_bh`` (FDR, at a stopping time). The e-BH procedure of Wang and Ramdas (2022,
   "False discovery rate control with e-values", JRSS-B): with weighted e-values
   ``e'_k = w_k e_k`` (``sum w_k = K``), let ``r`` be the largest ``j`` such that the
   ``j``-th largest ``e'`` is at least ``K / (alpha j)``; reject those ``r``. Proof of
   FDR control: with ``R`` rejections, a rejected ``k`` has ``e'_k >= K/(alpha R)``, so
   ``1{k rejected}/max(R, 1) <= alpha e'_k / K``; summing over true nulls and taking
   expectations, ``FDR <= alpha sum_{k null} w_k E[e_k] / K <= alpha``. No independence
   needed.

Caveat (stated, not hidden): (2) and (3) use the e-values at a single stopping time.
"Reject if e-BH ever rejects as time goes on" is not covered; use (1) for continuous
monitoring, and run (2) or (3) at the decision time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .sequential import Alternative, BettingEProcess


def _check(e_values: Sequence[float], alpha: float) -> None:
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie in (0, 1)")
    if any(e < 0 or math.isnan(e) for e in e_values):
        raise ValueError("e-values must be non-negative")


def normalise_weights(weights: Sequence[float] | None, k: int) -> list[float]:
    """Return weights rescaled to sum to ``k`` (uniform if ``None``)."""
    if weights is None:
        return [1.0] * k
    if len(weights) != k:
        raise ValueError("one weight per stratum")
    if any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("weights must be non-negative with a positive sum")
    total = sum(weights)
    return [w * k / total for w in weights]


def e_bonferroni(
    e_values: Sequence[float], alpha: float, weights: Sequence[float] | None = None
) -> tuple[int, ...]:
    """Indices with ``w_k e_k >= K / alpha`` (FWER <= alpha, any dependence)."""
    _check(e_values, alpha)
    k = len(e_values)
    w = normalise_weights(weights, k)
    return tuple(i for i in range(k) if w[i] * e_values[i] >= k / alpha)


def e_bh(
    e_values: Sequence[float], alpha: float, weights: Sequence[float] | None = None
) -> tuple[int, ...]:
    """e-BH rejections, sorted by index (FDR <= alpha, any dependence)."""
    _check(e_values, alpha)
    k = len(e_values)
    if k == 0:
        return ()
    w = normalise_weights(weights, k)
    weighted = [w[i] * e_values[i] for i in range(k)]
    order = sorted(range(k), key=lambda i: -weighted[i])
    r = 0
    for j in range(1, k + 1):
        if weighted[order[j - 1]] >= k / (alpha * j):
            r = j
    return tuple(sorted(order[:r]))


def e_closed_mean(e_values: Sequence[float], alpha: float) -> tuple[int, ...]:
    """Closed testing with mean intersection e-values (FWER <= alpha, any dependence)."""
    _check(e_values, alpha)
    k = len(e_values)
    thr = 1.0 / alpha
    out: list[int] = []
    for i in range(k):
        others = sorted(e_values[j] for j in range(k) if j != i)
        total = e_values[i]
        ok = total >= thr
        for s, e in enumerate(others, start=2):
            if not ok:
                break
            total += e
            ok = total / s >= thr
        if ok:
            out.append(i)
    return tuple(out)


@dataclass
class StratifiedMonitor:
    """One betting e-process per stratum, updated as cases arrive in any order."""

    strata: Sequence[str]
    null_means: Mapping[str, float] | float = 0.0
    alternative: Alternative = "greater"
    weights: Mapping[str, float] | None = None

    processes: dict[str, BettingEProcess] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if len(set(self.strata)) != len(self.strata) or not self.strata:
            raise ValueError("strata must be non-empty and unique")
        for s in self.strata:
            m = self.null_means if isinstance(self.null_means, float | int) else self.null_means[s]
            self.processes[s] = BettingEProcess(float(m), self.alternative)

    @property
    def k(self) -> int:
        return len(self.strata)

    def _weights(self) -> list[float] | None:
        if self.weights is None:
            return None
        return [self.weights[s] for s in self.strata]

    def update(self, stratum: str, x: float) -> None:
        try:
            self.processes[stratum].update(x)
        except KeyError:
            raise ValueError(f"unknown stratum {stratum!r}") from None

    def e_values(self) -> dict[str, float]:
        return {s: self.processes[s].e_value for s in self.strata}

    def counts(self) -> dict[str, int]:
        return {s: self.processes[s].n for s in self.strata}

    def fwer_rejections(self, alpha: float) -> tuple[str, ...]:
        """Time-uniform weighted e-Bonferroni on running maxima (valid at all times)."""
        maxima = [self.processes[s].max_e_value for s in self.strata]
        return tuple(self.strata[i] for i in e_bonferroni(maxima, alpha, self._weights()))

    def closed_rejections(self, alpha: float) -> tuple[str, ...]:
        """Closed mean testing on current e-values (valid at this stopping time)."""
        cur = [self.processes[s].e_value for s in self.strata]
        return tuple(self.strata[i] for i in e_closed_mean(cur, alpha))

    def fdr_rejections(self, alpha: float) -> tuple[str, ...]:
        """Weighted e-BH on current e-values (valid at this stopping time)."""
        cur = [self.processes[s].e_value for s in self.strata]
        return tuple(self.strata[i] for i in e_bh(cur, alpha, self._weights()))
