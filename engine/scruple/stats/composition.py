"""What error guarantee does the whole promote decision carry? (docs/math/08).

The gate promotes a candidate only if every conjunct passes: the hard checks on every
case (deterministic functions of the record), the statistical block family does not
fire, the certificate rejects "harmful", and (optionally) a soft-score threshold and
a Bayes rule agree. Theorem 8.1:

* **promote side (intersection-union).** If a conjunct ``j`` passes a harmful
  candidate with probability at most ``alpha_j`` whenever harm ``j`` holds, the
  composed rule promotes a candidate with harms ``J`` with probability at most
  ``min_{j in J} alpha_j``. No multiplicity correction: adding conjuncts can only make
  promotion rarer (Berger 1982, "Multiparameter hypothesis testing and acceptance
  sampling", Technometrics).
* **block side (union).** A candidate with no harm is blocked with probability at
  most ``alpha_block + 1 - prod_i (1 - phi_i)``, ``phi_i`` the hard checks'
  false-alarm probability on case ``i``. False blocks add up; false promotes do not.
* **resubmission.** With per-submission certificate levels ``alpha_k`` and
  ``sum_k alpha_k <= alpha``, the probability that any harmful version is ever
  promoted is at most ``alpha`` (union bound; ``spending_levels``).

``certify_every_type`` is the per-type certificate that the promote-side guarantee
needs to cover rare-type regressions: promote only if every case type is certified.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .sequential import BettingEProcess


def iut_promote_bound(levels: Mapping[str, float], harms: Sequence[str]) -> float:
    """Theorem 8.1(a): ``P(promote) <= min_{j in harms} alpha_j`` (1 if no harm is covered).

    ``levels`` maps a conjunct's harm to the conjunct's level for that harm (0 for a
    deterministic check that reads the violation on every case).
    """
    for k, a in levels.items():
        if not 0.0 <= a <= 1.0:
            raise ValueError(f"level for {k!r} must lie in [0, 1]")
    covered = [levels[h] for h in harms if h in levels]
    return min(covered) if covered else 1.0


def false_block_bound(alpha_block: float, false_alarms: Sequence[float]) -> float:
    """Theorem 8.1(b): ``alpha_block + 1 - prod (1 - phi_i)`` (capped at 1)."""
    if not 0.0 <= alpha_block <= 1.0:
        raise ValueError("alpha_block must lie in [0, 1]")
    log_pass = 0.0
    for phi in false_alarms:
        if not 0.0 <= phi <= 1.0:
            raise ValueError("false-alarm probabilities must lie in [0, 1]")
        if phi == 1.0:
            return 1.0
        log_pass += math.log1p(-phi)
    return min(1.0, alpha_block + 1.0 - math.exp(log_pass))


def spending_levels(
    alpha: float, k: int, kind: Literal["basel", "geometric"] = "basel"
) -> list[float]:
    """Levels for submissions ``1..k`` whose infinite sum is at most ``alpha``.

    ``basel``: ``alpha 6 / (pi^2 j^2)``; ``geometric``: ``alpha 2^-j``.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie in (0, 1)")
    if k < 0:
        raise ValueError("k must be non-negative")
    if kind == "basel":
        return [alpha * 6.0 / (math.pi**2 * j * j) for j in range(1, k + 1)]
    if kind == "geometric":
        return [alpha * 0.5**j for j in range(1, k + 1)]
    raise ValueError(f"unknown kind {kind!r}")


class TypeCertificates(BaseModel):
    model_config = ConfigDict(frozen=True)

    all_certified: bool
    max_e_values: dict[str, float]
    certified: tuple[str, ...]


def certify_every_type(
    stream: Sequence[tuple[str, float]],
    tolerances: Mapping[str, float],
    alpha: float,
) -> TypeCertificates:
    """Per-type certificates on one interleaved stream of ``(case type, x in [-1, 1])``.

    Type ``k`` is certified when its betting e-process for ``H0_k: mean_k >= t_k`` ever
    reaches ``1/alpha``. ``all_certified`` requires every type in ``tolerances``.
    Corollary 8.2: ``P(all_certified) <= alpha`` whenever any one type is harmful,
    with no division of ``alpha`` by the number of types.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie in (0, 1)")
    if not tolerances:
        raise ValueError("no case types")
    procs = {k: BettingEProcess(t, "less") for k, t in tolerances.items()}
    for k, x in stream:
        if k not in procs:
            raise ValueError(f"case type {k!r} has no declared tolerance")
        procs[k].update(x)
    certified = tuple(sorted(k for k, p in procs.items() if p.rejected(alpha)))
    return TypeCertificates(
        all_certified=len(certified) == len(procs),
        max_e_values={k: p.max_e_value for k, p in sorted(procs.items())},
        certified=certified,
    )


__all__ = [
    "TypeCertificates",
    "certify_every_type",
    "false_block_bound",
    "iut_promote_bound",
    "spending_levels",
]
