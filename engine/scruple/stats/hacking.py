"""Goodhart / reward-hacking gap under random spot-check audits.

Setting
-------
A candidate (agent change) is evaluated on ``n`` cases. On case ``i`` it yields a
*proxy* score ``P_i`` (the agent's self-report, or a naive win/loss label) and an
*anchored* score ``A_i`` (minus the ledger cost recomputed from journal postings,
which is what money actually did). Both are in normalised units. The discrepancy is
``D_i = P_i - A_i``, and by the declared cost cap ``0 <= D_i <= eps_max``. The
**hacking gap** of the candidate is

    Gamma = (1/n) sum_i D_i  =  (proxy mean) - (anchored mean),

i.e. how much better the candidate looks on the proxy than it is on the anchor. An
honest candidate has ``D_i <= eta`` (``eta`` absorbs measurement noise).

Audits. Each case is audited independently with probability ``q``; an audited case
with ``D_i > eta`` is detected with probability ``d`` (``d = 1`` for an exact
ledger recomputation). The audit draws are committed (seeded, operator-held) and are
independent of the candidate's behaviour: the agent cannot see which cases will be
audited. The gate accepts only if no audited case is flagged (zero tolerance, like
the gate's hard checks). Let ``M = #{i : D_i > eta}`` be the number of hacked cases
and ``r = 1 - q d``.

Theorem 1 (tail). For every policy, possibly randomised and adaptive across cases
(but not observing audit draws),

    P(accepted and Gamma >= gamma) <= r ** ceil(n (gamma - eta) / eps_max).

Proof. ``Gamma <= (M eps_max + (n - M) eta)/n <= eta + M eps_max / n``, so
``Gamma >= gamma`` forces ``M >= m0 := ceil(n (gamma - eta)/eps_max)``. Conditional
on the policy's choices (independent of the audits), each hacked case escapes
detection independently with probability ``r``, so
``P(accepted | M) = r^M <= r^{m0}`` on that event. Take expectations.

Theorem 2 (expected accepted gap).
``E[Gamma 1{accepted}] <= eta + eps_max / (n e ln(1/r))``.
Proof. ``E[Gamma 1{acc}] <= eta + (eps_max/n) E[M r^M]`` and
``sup_{x >= 0} x r^x = 1/(e ln(1/r))`` (attained at ``x = 1/ln(1/r)``).

So the optimiser's expected proxy-over-anchor gain that survives the gate shrinks
like ``1/(n q d)``. Theorems 1 and 2 hold for anchored *harm* as well: replace
``D_i`` by the per-case true loss increase ``h_i <= h_max``.

Theorem 3 (minimal audit rate). Cost model: an accepted candidate pays the optimiser
``V + sum_{i in S} g_i`` (honest value ``V >= 0`` plus proxy gain ``g_i <= g_max`` per
hacked case in the set ``S``); a rejected one pays ``-C`` (``C >= 0``: lost submission,
penalty, budget). The honest candidate is always accepted (``D_i <= eta``) and pays
``V``. Hacking ``S`` with ``|S| = m`` has expected utility
``EU(S) = r^m (V + sum_S g_i) - (1 - r^m) C``. Then

    EU(S) <= V for every S   iff   q d >= g_max / (V + C + g_max)

(the "iff" uses that some case offers gain ``g_max``). Proof. Write ``K = V + C``.
Necessity: ``m = 1`` on the best case gives ``r (K + g_max) <= K``. Sufficiency:
``EU(S) + C <= f(m) := r^m (K + m g_max)`` and
``f(m+1)/f(m) = r (K + (m+1) g_max)/(K + m g_max) <= r (K + g_max)/K <= 1``, so
``f`` is non-increasing and ``EU(S) <= f(0) - C = V``.

Corollary (resubmission). If a caught candidate costs nothing (``C = 0``) and has no
honest value (``V = 0``), then ``q* = 1``: spot checks alone cannot deter an agent
that may resubmit freely. Deterrence needs a cost per caught attempt, which is what a
submission budget (verifier gap G-1) supplies.

The deterrence condition ``q F >= gain`` is the classic one from the economics of
crime (Becker 1968, "Crime and Punishment: An Economic Approach", JPE) and
inspection games (Avenhaus, von Stengel and Zamir 2002, "Inspection Games",
Handbook of Game Theory). Theorems 1 and 2 (the ``r^M`` survival argument for
batch acceptance) are elementary; their use as a bound on the proxy-anchor gap of an
accepted agent change is our framing and needs a literature check.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field


class AuditModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rate: float = Field(ge=0.0, le=1.0)  # q
    detection: float = Field(default=1.0, gt=0.0, le=1.0)  # d
    tolerance: float = Field(default=0.0, ge=0.0)  # eta (normalised units)

    @property
    def survival(self) -> float:
        """``r = 1 - q d``: probability one hacked case escapes the audit."""
        return 1.0 - self.rate * self.detection


class Incentive(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    honest_value: float = Field(ge=0.0)  # V
    rejection_cost: float = Field(ge=0.0)  # C
    max_gain_per_case: float = Field(gt=0.0)  # g_max


def undetected_probability(hacked: int, audit: AuditModel) -> float:
    if hacked < 0:
        raise ValueError("hacked must be non-negative")
    return float(audit.survival**hacked)


def gap_tail_bound(gamma: float, n: int, eps_max: float, audit: AuditModel) -> float:
    """Theorem 1: ``P(accepted and Gamma >= gamma) <= r^ceil(n (gamma - eta)/eps_max)``."""
    if n <= 0 or eps_max <= 0:
        raise ValueError("n and eps_max must be positive")
    if gamma <= audit.tolerance:
        return 1.0
    m0 = math.ceil(n * (gamma - audit.tolerance) / eps_max - 1e-12)
    return undetected_probability(m0, audit)


def expected_gap_bound(n: int, eps_max: float, audit: AuditModel) -> float:
    """Theorem 2: ``E[Gamma 1{accepted}] <= eta + eps_max / (n e ln(1/r))`` (capped)."""
    if n <= 0 or eps_max <= 0:
        raise ValueError("n and eps_max must be positive")
    r = audit.survival
    if r >= 1.0:
        return audit.tolerance + eps_max
    if r <= 0.0:
        return audit.tolerance
    return min(
        audit.tolerance + eps_max / (n * math.e * math.log(1.0 / r)), audit.tolerance + eps_max
    )


def expected_utility(gains: Sequence[float], audit: AuditModel, inc: Incentive) -> float:
    """``EU(S) = r^m (V + sum g) - (1 - r^m) C`` for the hacked set with these gains."""
    p = undetected_probability(len(gains), audit)
    return p * (inc.honest_value + math.fsum(gains)) - (1.0 - p) * inc.rejection_cost


def minimal_audit_rate(inc: Incentive, detection: float = 1.0) -> float:
    """Theorem 3: smallest ``q`` making every hack unprofitable (may exceed 1: impossible)."""
    if not 0 < detection <= 1:
        raise ValueError("detection must lie in (0, 1]")
    g = inc.max_gain_per_case
    return g / (detection * (inc.honest_value + inc.rejection_cost + g))


def best_response(
    available_gains: Sequence[float], audit: AuditModel, inc: Incentive
) -> tuple[int, float]:
    """``(m*, EU*)``: the optimiser's best number of hacks (largest gains first)."""
    if any(g < 0 for g in available_gains):
        raise ValueError("gains must be non-negative")
    ordered = sorted(available_gains, reverse=True)
    best_m, best = 0, expected_utility([], audit, inc)
    running = 0.0
    for m, g in enumerate(ordered, start=1):
        running += g
        p = undetected_probability(m, audit)
        eu = p * (inc.honest_value + running) - (1.0 - p) * inc.rejection_cost
        if eu > best + 1e-12:
            best_m, best = m, eu
    return best_m, best


# --- simulation (synthetic) -------------------------------------------------------------

# A policy maps (per-case available inflation eps_i, rng) to the inflation it uses
# on each case (0 <= used_i <= eps_i). It never sees the audit draws.
Policy = Callable[[Sequence[float], random.Random], list[float]]


def honest() -> Policy:
    return lambda eps, rng: [0.0] * len(eps)


def hack_all() -> Policy:
    return lambda eps, rng: list(eps)


def hack_random(m: int) -> Policy:
    def run(eps: Sequence[float], rng: random.Random) -> list[float]:
        idx = [i for i, e in enumerate(eps) if e > 0]
        chosen = set(rng.sample(idx, min(m, len(idx))))
        return [e if i in chosen else 0.0 for i, e in enumerate(eps)]

    return run


def hack_largest(m: int) -> Policy:
    def run(eps: Sequence[float], rng: random.Random) -> list[float]:
        top = set(sorted(range(len(eps)), key=lambda i: -eps[i])[:m])
        return [e if i in top and e > 0 else 0.0 for i, e in enumerate(eps)]

    return run


def under_tolerance(eta: float) -> Policy:
    """Inflate every case by at most ``eta``: invisible to audits, gap <= eta."""
    return lambda eps, rng: [min(e, eta) for e in eps]


class RoundResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: bool
    gap: float  # Gamma
    hacked: int  # M
    proxy_gain: float  # sum of inflation used


def play_round(
    policy: Policy,
    eps: Sequence[float],
    audit: AuditModel,
    rng: random.Random,
) -> RoundResult:
    """One submission: the policy acts, then audits are drawn (independently)."""
    used = policy(eps, rng)
    if len(used) != len(eps) or any(u < 0 or u > e + 1e-12 for u, e in zip(used, eps, strict=True)):
        raise ValueError("policy must use 0 <= inflation <= available on each case")
    flagged = False
    for u in used:
        audited = rng.random() < audit.rate
        if audited and u > audit.tolerance and rng.random() < audit.detection:
            flagged = True
    hacked = sum(1 for u in used if u > audit.tolerance)
    return RoundResult(
        accepted=not flagged,
        gap=math.fsum(used) / len(used),
        hacked=hacked,
        proxy_gain=math.fsum(used),
    )


def available_inflation(n: int, hackable: float, eps_max: float, rng: random.Random) -> list[float]:
    """Per-case available inflation: ``U(0, eps_max)`` on a ``hackable`` share, else 0."""
    return [rng.uniform(0.0, eps_max) if rng.random() < hackable else 0.0 for _ in range(n)]


def learning_optimiser(
    eps: Sequence[float],
    audit: AuditModel,
    inc: Incentive,
    *,
    rounds: int,
    arms: Sequence[int],
    seed: int,
    explore: float = 0.1,
) -> tuple[float, int]:
    """An epsilon-greedy optimiser choosing how many cases to hack, round after round.

    It only observes its own payoffs (accepted: ``V + gain``; rejected: ``-C``).
    Returns ``(mean payoff per round, most chosen arm)``.
    """
    rng = random.Random(seed)
    totals = dict.fromkeys(arms, 0.0)
    counts = dict.fromkeys(arms, 0)
    payoff_sum = 0.0
    for t in range(rounds):
        if t < len(arms):
            arm = arms[t]
        elif rng.random() < explore:
            arm = rng.choice(list(arms))
        else:
            arm = max(arms, key=lambda a: totals[a] / max(counts[a], 1))
        res = play_round(hack_largest(arm), eps, audit, rng)
        pay = inc.honest_value + res.proxy_gain if res.accepted else -inc.rejection_cost
        totals[arm] += pay
        counts[arm] += 1
        payoff_sum += pay
    return payoff_sum / rounds, max(arms, key=lambda a: counts[a])
