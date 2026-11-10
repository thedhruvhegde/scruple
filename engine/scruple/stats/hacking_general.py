"""Hacking-gap bounds with leaky audits and an imperfect verifier (docs/math/05, 5.5-5.8).

Extends ``hacking.py`` (Theorems 5.1-5.4) in three directions:

1. **Adaptive, stochastic policies with partial information about the audits.** The
   only assumption is a floor ``rho`` on the conditional probability that a hacked
   case is flagged, given everything the agent knew when it hacked it (Theorem 5.5).
   No independence between cases, audits or detections is needed.
2. **Leaky audits.** If the agent sees a signal whose likelihood ratio
   ``P(s | audited) / P(s | not audited)`` is at least ``l`` for every ``s``, the floor
   is ``rho = (1 - beta) q l / (q l + 1 - q)`` (Corollary 5.6).
3. **An imperfect verifier**: false-negative rate ``beta`` on audited hacked cases and
   false-alarm rate ``phi`` on audited honest cases. The minimal audit rate that makes
   every hack unprofitable is ``q* = g / ((1 - beta)(K + g) - phi K)`` with
   ``K = V + C`` (Theorem 5.7); if ``beta g >= (1 - beta - phi) K`` no audit rate in
   [0, 1] deters (Corollary 5.8).

Synthetic simulation helpers at the bottom replay the E2 policies' hacked counts.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LeakyAudit(BaseModel):
    """Audit rate ``q``, verifier false-negative ``beta`` and false-alarm ``phi``,
    and a floor ``leak`` on the likelihood ratio of what the agent observes
    (``leak = 1``: the agent learns nothing; ``leak = 0``: it may see the audits)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rate: float = Field(ge=0.0, le=1.0)  # q
    false_negative: float = Field(default=0.0, ge=0.0, le=1.0)  # beta
    false_alarm: float = Field(default=0.0, ge=0.0, lt=1.0)  # phi
    leak: float = Field(default=1.0, ge=0.0, le=1.0)  # l
    tolerance: float = Field(default=0.0, ge=0.0)  # eta

    @model_validator(mode="after")
    def _informative(self) -> LeakyAudit:
        if self.false_alarm > 1.0 - self.false_negative + 1e-12:
            raise ValueError(
                "false_alarm above the detection rate: the verifier is worse than a coin"
            )
        return self

    @property
    def audit_posterior_floor(self) -> float:
        """Smallest ``P(audited | signal)``: ``q l / (q l + 1 - q)`` (Corollary 5.6)."""
        q, ell = self.rate, self.leak
        den = q * ell + 1.0 - q
        return 0.0 if den <= 0.0 else q * ell / den

    @property
    def catch_floor(self) -> float:
        """``rho``: floor on ``P(flagged | hacked, agent's information)``."""
        return (1.0 - self.false_negative) * self.audit_posterior_floor


def tail_bound(gamma: float, n: int, eps_max: float, rho: float, eta: float = 0.0) -> float:
    """Theorem 5.5: ``P(acc and Gamma >= gamma) <= (1 - rho)^ceil(n (gamma - eta)/eps_max)``."""
    _check(n, eps_max, rho)
    if gamma <= eta:
        return 1.0
    m0 = math.ceil(n * (gamma - eta) / eps_max - 1e-12)
    return float((1.0 - rho) ** m0)


def expected_gap_bound(n: int, eps_max: float, rho: float, eta: float = 0.0) -> float:
    """Theorem 5.5: ``E[Gamma 1{accepted}] <= eta + eps_max / (n e ln(1/(1 - rho)))``."""
    _check(n, eps_max, rho)
    if rho <= 0.0:
        return eta + eps_max
    if rho >= 1.0:
        return eta
    return min(eta + eps_max / (n * math.e * -math.log1p(-rho)), eta + eps_max)


def _check(n: int, eps_max: float, rho: float) -> None:
    if n <= 0 or eps_max <= 0:
        raise ValueError("n and eps_max must be positive")
    if not 0.0 <= rho <= 1.0:
        raise ValueError("rho must lie in [0, 1]")


def acceptance_probability(n: int, hacked: int, audit: LeakyAudit) -> float:
    """``(1 - q phi)^(n - m) (1 - q (1 - beta))^m`` (independent audits, no leak)."""
    if not 0 <= hacked <= n:
        raise ValueError("need 0 <= hacked <= n")
    q = audit.rate
    honest = 1.0 - q * audit.false_alarm
    caught = 1.0 - q * (1.0 - audit.false_negative)
    return float(honest ** (n - hacked) * caught**hacked)


def expected_utility(
    n: int, gains: Sequence[float], audit: LeakyAudit, honest_value: float, rejection_cost: float
) -> float:
    """``P(acc)(V + sum g) - (1 - P(acc)) C`` for hacking cases with these gains."""
    p = acceptance_probability(n, len(gains), audit)
    return p * (honest_value + math.fsum(gains)) - (1.0 - p) * rejection_cost


def minimal_audit_rate(
    honest_value: float,
    rejection_cost: float,
    max_gain: float,
    *,
    false_negative: float = 0.0,
    false_alarm: float = 0.0,
) -> float:
    """Theorem 5.7: ``q* = g / ((1 - beta)(K + g) - phi K)``; ``inf`` when nothing deters.

    A value above 1 also means no feasible audit rate deters (Corollary 5.8).
    """
    if honest_value < 0 or rejection_cost < 0 or max_gain <= 0:
        raise ValueError("need V >= 0, C >= 0, g > 0")
    if not 0.0 <= false_negative <= 1.0 or not 0.0 <= false_alarm < 1.0:
        raise ValueError("rates must lie in [0, 1]")
    k = honest_value + rejection_cost
    den = (1.0 - false_negative) * (k + max_gain) - false_alarm * k
    return math.inf if den <= 0.0 else max_gain / den


def deterrable(
    honest_value: float,
    rejection_cost: float,
    max_gain: float,
    *,
    false_negative: float = 0.0,
    false_alarm: float = 0.0,
) -> bool:
    """Corollary 5.8: some ``q <= 1`` deters iff ``beta g <= (1 - beta - phi) K``."""
    k = honest_value + rejection_cost
    return false_negative * max_gain <= (1.0 - false_negative - false_alarm) * k + 1e-12


def best_response(
    n: int,
    available_gains: Sequence[float],
    audit: LeakyAudit,
    honest_value: float,
    rejection_cost: float,
) -> tuple[int, float]:
    """``(m*, EU*)`` over hacking the ``m`` largest gains (optimal for a fixed ``m``)."""
    ordered = sorted(available_gains, reverse=True)
    best_m = 0
    best = expected_utility(n, [], audit, honest_value, rejection_cost)
    for m in range(1, len(ordered) + 1):
        eu = expected_utility(n, ordered[:m], audit, honest_value, rejection_cost)
        if eu > best + 1e-15:
            best_m, best = m, eu
    return best_m, best


def leak_aware_outcomes(n: int, hackable: int, audit: LeakyAudit) -> list[float]:
    """Exact ``P(accepted and H = h)``, ``h = 0..hackable``, for the leak-aware policy of
    ``play_leaky_round`` (hack a hackable case unless its signal is ``"hi"``).

    Per hackable case: audited with signal "hi" (not hacked; flagged w.p. ``phi``),
    audited with "lo" (hacked; flagged w.p. ``1 - beta``), not audited (hacked, never
    flagged). The other ``n - hackable`` cases are honest (flagged w.p. ``q phi``).
    """
    if not 0 <= hackable <= n:
        raise ValueError("need 0 <= hackable <= n")
    q, ell = audit.rate, audit.leak
    beta, phi = audit.false_negative, audit.false_alarm
    a = q * (1.0 - ell) * (1.0 - phi)
    b = q * ell * beta + (1.0 - q)
    rest = (1.0 - q * phi) ** (n - hackable)
    m = hackable
    return [rest * math.comb(m, h) * a ** (m - h) * b**h for h in range(m + 1)]


# --- simulation (synthetic) -------------------------------------------------------------


class LeakyRound(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: bool
    hacked: int
    gap: float


def play_leaky_round(
    n: int,
    hackable: Sequence[int],
    audit: LeakyAudit,
    rng: random.Random,
    *,
    leak_aware: bool,
    eps: float = 1.0,
) -> LeakyRound:
    """One batch of ``n`` cases; the policy may hack the cases in ``hackable``.

    Each case is audited with probability ``q``. The agent sees a binary signal:
    ``"hi"`` only on audited cases (with probability ``1 - leak``), ``"lo"``
    otherwise, so ``P(lo | audited) / P(lo | not audited) = leak`` (the floor) and
    the ``"hi"`` ratio is infinite. A leak-aware policy hacks only on ``"lo"``; a naive
    one hacks every hackable case. Audited hacked cases are flagged with probability
    ``1 - beta``, audited honest cases with probability ``phi``. Any flag rejects.
    """
    hack_set = set(hackable)
    if any(not 0 <= i < n for i in hack_set):
        raise ValueError("hackable indices must lie in [0, n)")
    flagged = False
    hacked = 0
    for i in range(n):
        audited = rng.random() < audit.rate
        signal_hi = audited and rng.random() >= audit.leak
        hack = i in hack_set and not (leak_aware and signal_hi)
        hacked += hack
        if audited:
            p_flag = (1.0 - audit.false_negative) if hack else audit.false_alarm
            if rng.random() < p_flag:
                flagged = True
    return LeakyRound(accepted=not flagged, hacked=hacked, gap=hacked * eps / n)


def learning_optimiser(
    n: int,
    gain: float,
    max_hacks: int,
    audit: LeakyAudit,
    honest_value: float,
    rejection_cost: float,
    *,
    rounds: int,
    seed: int,
    explore: float = 0.1,
    arms: Sequence[int] | None = None,
) -> tuple[float, int]:
    """Epsilon-greedy choice of how many cases to hack (gain ``gain`` each).

    Each round simulates the batch (false alarms included); the optimiser sees only
    its payoff. Returns ``(mean payoff, most chosen arm)``.
    """
    rng = random.Random(seed)
    choices = list(arms) if arms is not None else sorted({0, 1, 2, 5, 10, 20, 50, max_hacks})
    choices = [a for a in choices if 0 <= a <= max_hacks]
    totals = dict.fromkeys(choices, 0.0)
    counts = dict.fromkeys(choices, 0)
    total = 0.0
    for t in range(rounds):
        if t < len(choices):
            arm = choices[t]
        elif rng.random() < explore:
            arm = rng.choice(choices)
        else:
            arm = max(choices, key=lambda a: totals[a] / max(counts[a], 1))
        res = play_leaky_round(n, range(arm), audit, rng, leak_aware=False)
        pay = honest_value + arm * gain if res.accepted else -rejection_cost
        totals[arm] += pay
        counts[arm] += 1
        total += pay
    return total / rounds, max(choices, key=lambda a: counts[a])


__all__ = [
    "LeakyAudit",
    "LeakyRound",
    "acceptance_probability",
    "best_response",
    "deterrable",
    "expected_gap_bound",
    "expected_utility",
    "leak_aware_outcomes",
    "learning_optimiser",
    "minimal_audit_rate",
    "play_leaky_round",
    "tail_bound",
]
