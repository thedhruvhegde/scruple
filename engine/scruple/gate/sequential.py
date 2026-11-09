"""Sequential two-sample canary test: anytime-valid, stops early on clear harm or benefit.

Pure. Used by the one-shot canary and the staged ramp when GateConfig.stats = "evalue".

The limitation it addresses
---------------------------
The canary arms are matched groups, not paired cases: a live case is decided by the
candidate or by the baseline, never both, so there is no per-case loss difference
and the paired betting test of docs/math/01 does not apply. The design gives us
something else instead: among the matched cases (the ones the candidate would
decide, a function of observable fields only), each case is sent to the candidate
with a known probability ``p`` (the canary percentage), independently of its
outcome.

Derivation (inverse-propensity betting)
---------------------------------------
Case ``i`` has potential costs ``Y_i(1)`` (candidate) and ``Y_i(0)`` (baseline) in
``[0, c]`` (declared cap ``c`` of its case type), and an assignment
``T_i ~ Bernoulli(p)`` drawn independently of everything before it and of the
potential costs. Only ``Y_i = Y_i(T_i)`` is seen. The inverse-propensity term

    Z_i = Y_i (T_i / p - (1 - T_i) / (1 - p))

satisfies ``E[Z_i | F_{i-1}, Y_i(0), Y_i(1)] = Y_i(1) - Y_i(0)`` (Horvitz and
Thompson 1952), so ``E[Z_i | F_{i-1}] = tau_i``, the expected effect of the
candidate on case ``i``'s cost given the past. ``Z_i`` lies in ``[-c/(1-p), c/p]``,
so ``X_i = Z_i / M`` with ``M = c max(1/p, 1/(1-p))`` lies in ``[-1, 1]`` and
``E[X_i | F_{i-1}] = tau_i / M``. That is exactly the setting of
``scruple.stats.BettingEProcess`` (conditional-mean nulls for bounded observations):

* harm: ``H0_k: tau_i <= t`` for every type-``k`` case (null mean ``t / M``);
  rejecting means the candidate costs more than control by more than the
  tolerance ``t``: roll back;
* benefit: ``H0_k: tau_i >= 0``; rejecting means the candidate is cheaper: pass
  early.

Each is a non-negative supermartingale under its null (docs/math/01), so by
Ville's inequality it may be checked after every case and stopped at any time.
One process per case type (the shadow gate's ``scenario/amount band``, each with
its own cap), combined by e-Bonferroni on running maxima: P(false rollback) <=
alpha and P(false early pass) <= alpha, under any dependence between types
(docs/math/02, Proposition 2.2). Per type, ``BettingConfidenceSequence`` on
``X`` gives a time-uniform interval for the mean effect (i.i.d. cases).

This is a standard inverse-propensity construction composed with the betting
e-process; we found no reference stating it for canary releases specifically
(short search, not a literature review). It is checked by seeded Monte Carlo in
tests/test_gate_sequential.py.

Assumptions (also in docs/DESIGN.md)
------------------------------------
* Assignment is a hash of ``case_id`` and the candidate's content hash
  (``canary.assign``). We treat it as an independent Bernoulli(``p``) draw: it does
  not depend on outcomes, but it is pseudo-random, not random. ASSUMPTION.
* ``p`` is known exactly: ``basis_points(percent) / 10,000``.
* Costs lie in ``[0, cap]`` for the case's type; a cost outside fails closed.
* The price of validity is variance: ``Z`` scales with ``1/p``, so at a 10%
  canary the test needs many matched cases to see small effects. When it neither
  rolls back nor certifies benefit, the canary passes on "no evidence of harm",
  which is the same standard as the bootstrap path, now with error control on the
  rollback side only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from scruple.sim.models import DisputeCase
from scruple.stats import BettingConfidenceSequence, BettingEProcess, e_bonferroni

from .config import GateConfig
from .evalue import case_type

if TYPE_CHECKING:
    from .models import CheckResult

METHOD = "inverse-propensity betting e-process per case type, e-Bonferroni (time-uniform)"


class Observation(NamedTuple):
    """One matched live case, in arrival order. Cost in USD minor units."""

    case_type: str
    cap: float
    treated: bool
    cost: float


class CanaryTypeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_type: str
    n: int  # matched cases seen before the stop
    treated: int
    cap: float
    scale: float  # M = cap * max(1/p, 1/(1-p))
    effect_estimate: float  # inverse-propensity mean of treatment minus control cost
    ci_low: float  # time-uniform confidence sequence for the effect, at alpha
    ci_high: float
    harm_e_value: float  # running maximum, H0: effect <= tolerance
    benefit_e_value: float  # running maximum, H0: effect >= 0


class SequentialCanaryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: str
    alpha: float
    tolerance_usd_minor: float
    assignment_probability: float
    decision: Literal["harm", "benefit", "no_decision"]
    stopped_at: int  # matched cases used (the first crossing, or all of them)
    n_matched: int
    e_threshold: float  # K / alpha: the running maximum that stops on one type
    harm: tuple[str, ...]
    benefit: tuple[str, ...]
    types: tuple[CanaryTypeEvidence, ...]


def scale(cap: float, p: float) -> float:
    return cap * max(1.0 / p, 1.0 / (1.0 - p))


def ipw_term(cost: float, treated: bool, p: float) -> float:
    """``Z = Y (T/p - (1-T)/(1-p))``: unbiased for the per-case effect."""
    return cost / p if treated else -cost / (1.0 - p)


def sequential_canary(
    obs: Sequence[Observation],
    p: float,
    alpha: float,
    tolerance: float = 0.0,
    *,
    stop_early: bool = True,
) -> SequentialCanaryResult:
    """Monitor matched cases in order; stop at the first harm or benefit crossing.

    Raises ``ValueError`` on a cost outside ``[0, cap]`` or an invalid ``p``.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("assignment probability must lie in (0, 1)")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie in (0, 1)")
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    caps: dict[str, float] = {}
    for o in obs:
        if caps.setdefault(o.case_type, o.cap) != o.cap:
            raise ValueError(f"case type {o.case_type!r} has two different caps")
        if not 0.0 <= o.cost <= o.cap:
            raise ValueError(f"cost {o.cost} outside [0, {o.cap}] for {o.case_type!r}")
    types = sorted(caps)
    scales = {t: scale(caps[t], p) for t in types}
    for t in types:
        if tolerance / scales[t] >= 1.0:
            raise ValueError(f"tolerance must be below the scale {scales[t]:g} of {t!r}")
    harm = {t: BettingEProcess(tolerance / scales[t], "greater") for t in types}
    benefit = {t: BettingEProcess(0.0, "less") for t in types}
    css = {t: BettingConfidenceSequence(alpha) for t in types}
    sums: dict[str, float] = defaultdict(float)
    treated: dict[str, int] = defaultdict(int)
    k = len(types)
    decision: Literal["harm", "benefit", "no_decision"] = "no_decision"
    flagged_harm: tuple[str, ...] = ()
    flagged_benefit: tuple[str, ...] = ()
    used = 0
    for o in obs:
        z = ipw_term(o.cost, o.treated, p)
        x = max(-1.0, min(1.0, z / scales[o.case_type]))
        harm[o.case_type].update(x)
        benefit[o.case_type].update(x)
        css[o.case_type].update(x)
        sums[o.case_type] += z
        treated[o.case_type] += o.treated
        used += 1
        flagged_harm = tuple(
            types[i] for i in e_bonferroni([harm[t].max_e_value for t in types], alpha)
        )
        flagged_benefit = tuple(
            types[i] for i in e_bonferroni([benefit[t].max_e_value for t in types], alpha)
        )
        if flagged_harm:
            decision = "harm"
        elif flagged_benefit:
            decision = "benefit"
        if decision != "no_decision" and stop_early:
            break
    lines = []
    for t in types:
        n = harm[t].n
        lo, hi = css[t].interval() if n else (-1.0, 1.0)
        lines.append(
            CanaryTypeEvidence(
                case_type=t,
                n=n,
                treated=treated[t],
                cap=caps[t],
                scale=scales[t],
                effect_estimate=sums[t] / n if n else 0.0,
                ci_low=lo * scales[t],
                ci_high=hi * scales[t],
                harm_e_value=harm[t].max_e_value,
                benefit_e_value=benefit[t].max_e_value,
            )
        )
    return SequentialCanaryResult(
        method=METHOD,
        alpha=alpha,
        tolerance_usd_minor=tolerance,
        assignment_probability=p,
        decision=decision,
        stopped_at=used,
        n_matched=len(obs),
        e_threshold=k / alpha if k else 0.0,
        harm=flagged_harm,
        benefit=flagged_benefit if decision != "harm" else (),
        types=tuple(lines),
    )


def observations(
    cases: Sequence[DisputeCase],
    matched: Collection[str],
    treated: Collection[str],
    costs: Mapping[str, int],
    cfg: GateConfig,
) -> list[Observation]:
    """Matched cases in arrival (generation) order, typed and capped as in shadow."""
    out: list[Observation] = []
    for c in cases:
        if c.case_id not in matched:
            continue
        t, cap = case_type(c, cfg)
        out.append(Observation(t, cap, c.case_id in treated, float(costs[c.case_id])))
    return out


def cost_violations(obs: Sequence[Observation]) -> list[str]:
    return [
        f"cost {o.cost:g} outside [0, {o.cap:g}] ({o.case_type})"
        for o in obs
        if not 0.0 <= o.cost <= o.cap
    ]


def describe(res: SequentialCanaryResult) -> str:
    if res.decision == "harm":
        return (
            f"harm detected after {res.stopped_at} of {res.n_matched} matched cases: "
            + ", ".join(
                f"'{t.case_type}' effect {t.effect_estimate / 100:+,.2f} USD/case "
                f"(e-value {t.harm_e_value:.3g} >= {res.e_threshold:.3g})"
                for t in res.types
                if t.case_type in res.harm
            )
        )
    if res.decision == "benefit":
        return (
            f"candidate certified cheaper after {res.stopped_at} of {res.n_matched} matched "
            f"cases: {', '.join(res.benefit)}"
        )
    worst = max(res.types, key=lambda t: t.harm_e_value, default=None)
    tail = (
        f"; largest harm e-value {worst.harm_e_value:.3g} ('{worst.case_type}', "
        f"stops at {res.e_threshold:.3g})"
        if worst is not None
        else ""
    )
    return f"no evidence of harm or benefit in {res.n_matched} matched cases{tail}"


def sequential_checks(
    obs: Sequence[Observation], p: float, alpha: float, tolerance: float
) -> tuple[tuple[CheckResult, ...], SequentialCanaryResult | None]:
    """The checks that replace the point-estimate tolerance rule in a canary."""
    from .models import CheckResult  # models imports this module's result types

    bad = cost_violations(obs)
    if bad:
        return (
            CheckResult(
                name="canary_declared_caps",
                kind="hard",
                passed=False,
                detail=f"{len(bad)} violation(s): {bad[0]}",
            ),
        ), None
    res = sequential_canary(obs, p, alpha, tolerance)
    return (
        CheckResult(
            name="canary_sequential_no_harm",
            kind="hard",
            passed=res.decision != "harm",
            detail=f"anytime-valid two-sample test at {alpha:g} "
            f"(tolerance {tolerance / 100:,.2f} USD/case, p = {p:g}): {describe(res)}",
        ),
    ), res
