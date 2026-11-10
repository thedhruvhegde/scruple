"""Per-case-type e-value gate for shadow replay (used when GateConfig.stats = "evalue").

Pure. Replaces the zero-tolerance slice check and the bootstrap interval checks with
anytime-valid statistics from `scruple.stats` (docs/math/01, 02, 06).

Case types. A case type is ``scenario/amount band``. Each band has a declared cost
cap, fixed from the observable amount before any outcome is seen: band ``<$X`` has
cap ``X + fees``, the top band ``GateConfig.evalue_cost_cap_usd_minor`` (an
ASSUMPTION about how far a policy change can move one case's cost; a case that
breaks its cap fails the gate closed, never clipped).

Family. Only case types where the candidate changes at least one decision are
tested. Which decisions change is a function of observable fields only (both
policies are deterministic functions of the case view), so the family is fixed
before outcomes and the procedures below are valid conditionally on it. On a case
whose decision did not change the loss cannot change either (same action, same
case); the engine checks this invariant and fails closed if it is broken.

Regression (block). For each type ``k`` in the family, a betting e-process for
``H0_k: mean_k(candidate - baseline) <= t_k`` (per-type tolerance ``t_k``),
evaluated at the end of the batch (a stopping time). e-BH at ``evalue_alpha`` over
the family (Wang and Ramdas 2022) flags the regressed types. FDR <= alpha under
any dependence; when no type regressed, P(any flag) = FDR <= alpha, so the
false-block rate is controlled. The zero-tolerance slice check has no error
control and false-blocks 73% to 100% of the time at the null boundary (gap G-7,
docs/math/02_stratified_guarantees.md).

Improvement (certificate). For each type, a betting e-process for
``H0_k: mean_k >= 0``; e-BH at ``evalue_alpha_certify`` flags the improved types.
If no type improves, P(any certified) <= alpha_certify.

Sample size (before outcomes). For each type in the family, ``scruple.stats.plan``
(docs/math/03, Bernstein bound) gives the number of type-``k`` cases ``n_k*`` that
flags a regression of ``t_k + MDR_k`` with power ``1 - beta`` at level
``alpha / K``, assuming the per-case sd is at most the configured fraction of the
cap. It uses only counts, caps and configuration. If any ``n_k < n_k*`` the verdict
is "insufficient evidence" (HOLD) with the required numbers: a small held-out set
never passes silently. The sd assumption is checked after the run and reported.

Reported for information: the adapter's pooled certificate and confidence
sequence, and its time-uniform e-Bonferroni flags (scruple.stats.evaluate_pairs).

Variance reduction (``GateConfig.stats = "evalue_vr"``, docs/math/07). A share ``w``
of the false-block budget goes to one stratified betting e-process over the family
(``scruple.stats.StratifiedBettingEProcess``: one wealth, one adaptive bet per case
type, for the intersection null "no tested type regressed beyond its tolerance");
e-BH runs at ``(1 - w) alpha``. A block needs either, so P(false block) <= alpha
still. The stratified test pays no ``K``-fold threshold and adds up evidence that
is spread over several types; e-BH still names the types. The plan takes, per type,
the smaller of the two paths' requirements (``scruple.stats.stratified_cases_needed``
at ``w alpha``, the Bernstein plan at ``(1 - w) alpha / K``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer

from scruple.sim.models import DisputeCase, to_usd
from scruple.stats import (
    BettingEProcess,
    LossPair,
    StatsGateConfig,
    StratifiedBettingEProcess,
    StratumSpec,
    e_bh,
    evaluate_pairs,
    plan,
    stratified_cases_needed,
)
from scruple.stats.planning import total_cases_for_count
from scruple.verifiers.outcome import OutcomeScores

from .config import GateConfig

METHOD = "betting e-process per case type, e-BH (Wang and Ramdas 2022)"
METHOD_VR = METHOD + ", plus a stratified betting e-process over the family (docs/math/07)"


def _dollars(minor: int) -> str:
    return f"${minor // 100:,}" if minor % 100 == 0 else f"${minor / 100:,.2f}"


def case_type(case: DisputeCase, cfg: GateConfig) -> tuple[str, float]:
    """``(case type, declared cap)`` from observable fields (amount) and the scenario."""
    amount = to_usd(case.amount_minor, case.dispute_currency)
    for edge in cfg.evalue_band_edges_usd_minor:
        if amount < edge:
            return f"{case.scenario.value}/<{_dollars(edge)}", float(
                edge + cfg.evalue_fees_usd_minor
            )
    top = cfg.evalue_band_edges_usd_minor[-1]
    return f"{case.scenario.value}/>={_dollars(top)}", cfg.evalue_cost_cap_usd_minor


def tolerance_for(t: str, cfg: GateConfig) -> float:
    tol = cfg.evalue_type_tolerances_usd_minor
    if t in tol:
        return tol[t]
    return tol.get(t.split("/", 1)[0], cfg.evalue_tolerance_usd_minor)


class TypeEvidence(BaseModel):
    """One case type. Money in USD minor units; diff = candidate minus baseline
    loss (positive = worse)."""

    model_config = ConfigDict(frozen=True)

    case_type: str
    n: int
    changed: int  # cases whose decision changed
    cap: float
    tolerance: float
    mean_diff: float
    sd: float
    regression_e_value: float  # H0: mean <= tolerance, at the end of the batch
    regression_max_e_value: float
    improvement_e_value: float  # H0: mean >= 0, at the end of the batch
    ci_low: float  # time-uniform confidence sequence at evalue_alpha
    ci_high: float
    alpha_share: float  # alpha / K: this type's share of the false-block budget
    e_threshold: float  # K / alpha: the e-value that flags this type on its own
    regressed: bool  # e-BH at evalue_alpha
    improved: bool  # e-BH at evalue_alpha_certify
    # sample-size plan (None when not planned)
    min_detectable_regression: float | None = None
    planning_sd: float | None = None  # ASSUMPTION used by the plan
    required_n: int | None = None  # n_k*: type cases needed for power 1 - beta
    sd_within_assumption: bool | None = None  # observed sd <= planning sd


class StratifiedEvidence(BaseModel):
    """The stratified betting e-process over the tested family (``evalue_vr`` only)."""

    model_config = ConfigDict(frozen=True)

    share: float  # w: its share of the false-block budget
    alpha: float  # w * evalue_alpha
    e_value: float  # running maximum
    threshold: float  # 1 / (w alpha)
    rejected: bool
    contributions: dict[str, float]  # log-wealth by case type


class EValueEvidence(BaseModel):
    """Everything the e-value gate decided on. Stored with the decision."""

    model_config = ConfigDict(frozen=True)

    method: str
    alpha: float
    alpha_certify: float
    n_cases: int
    family: tuple[str, ...]  # tested case types (candidate changes a decision there)
    untouched_types: int
    types: tuple[TypeEvidence, ...]
    regressed: tuple[str, ...]
    improved: tuple[str, ...]
    regressed_fwer: tuple[str, ...]  # adapter: time-uniform e-Bonferroni (information)
    pooled_mean_diff: float
    pooled_ci_low: float  # time-uniform, at 1 - confidence
    pooled_ci_high: float
    pooled_certify_e_value: float  # H0: pooled mean >= 0 (pooled cap, information)
    pooled_regression_e_value: float
    violations: tuple[str, ...]  # declared-cap or unchanged-decision violations
    # sample-size plan
    beta: float | None = None
    plan_method: str | None = None
    required_total_cases: int | None = None  # held-out size the plan asks for
    sufficient: bool = True  # every family type has n_k >= n_k*
    insufficient: tuple[str, ...] = ()
    stratified: StratifiedEvidence | None = None  # set when GateConfig.stats = "evalue_vr"

    @model_serializer(mode="wrap")
    def _drop_unset_stratified(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        out: dict[str, object] = handler(self)
        if out.get("stratified") is None:
            out.pop("stratified", None)
        return out

    @property
    def blocks(self) -> bool:
        """A regression was found: a type flagged by e-BH, or the stratified test."""
        return bool(self.regressed) or (self.stratified is not None and self.stratified.rejected)


def stratified_test(
    pairs: Sequence[LossPair],
    caps: Mapping[str, float],
    tolerances: Mapping[str, float],
    family: Sequence[str],
    *,
    alpha: float,
    alpha_certify: float,
    confidence: float,
    cost_cap: float,
    changed: Mapping[str, int] | None = None,
    stratified_share: float | None = None,
) -> EValueEvidence:
    """The pure statistical core: loss pairs in, per-type evidence out.

    ``family`` lists the case types to test (all others must have zero diffs).
    Raises ``ValueError`` if a pair breaks its declared cap. With
    ``stratified_share = w`` e-BH runs at ``(1 - w) alpha`` and a stratified betting
    e-process over the family at ``w alpha`` (``evalue_vr``).
    """
    if stratified_share is not None and not 0.0 < stratified_share < 1.0:
        raise ValueError("stratified_share must lie in (0, 1)")
    fam = sorted(set(family))
    ebh_alpha = alpha if stratified_share is None else (1.0 - stratified_share) * alpha
    cap_all = max([cost_cap, *caps.values()])
    sv = evaluate_pairs(
        pairs,
        StatsGateConfig(
            cost_cap=cap_all,
            type_caps={t: caps[t] for t in {p.case_type for p in pairs}},
            type_tolerances={t: tolerances.get(t, 0.0) for t in fam},
            alpha_block=alpha,
            alpha_certify=1.0 - confidence,
        ),
    )
    by_type = {t.case_type: t for t in sv.types}
    diffs: dict[str, list[float]] = defaultdict(list)
    improve = {t: BettingEProcess(0.0, "less") for t in fam}
    for p in pairs:
        diffs[p.case_type].append(p.diff)
        if p.case_type in improve:
            improve[p.case_type].update(max(-1.0, min(1.0, p.diff / caps[p.case_type])))
    present = [t for t in fam if t in by_type]
    k = len(present)
    reg_flags = {present[i] for i in e_bh([by_type[t].e_value for t in present], ebh_alpha)}
    strat_ev: StratifiedEvidence | None = None
    if stratified_share is not None:
        sp = StratifiedBettingEProcess(
            "greater", null_means={t: tolerances.get(t, 0.0) / caps[t] for t in present}
        )
        fam_set = set(present)
        for p in pairs:  # arrival order, as the per-type processes saw them
            if p.case_type in fam_set:
                sp.update(p.case_type, max(-1.0, min(1.0, p.diff / caps[p.case_type])))
        level = stratified_share * alpha
        strat_ev = StratifiedEvidence(
            share=stratified_share,
            alpha=level,
            e_value=sp.max_e_value,
            threshold=1.0 / level,
            rejected=sp.rejected(level),
            contributions=dict(sorted(sp.contributions.items())),
        )
    imp_flags = {present[i] for i in e_bh([improve[t].e_value for t in present], alpha_certify)}
    lines = []
    for t in present:
        tv = by_type[t]
        xs = diffs[t]
        mean = math.fsum(xs) / len(xs)
        sd = math.sqrt(math.fsum((x - mean) ** 2 for x in xs) / len(xs))
        lines.append(
            TypeEvidence(
                case_type=t,
                n=tv.n,
                changed=(changed or {}).get(t, 0),
                cap=tv.cap,
                tolerance=tolerances.get(t, 0.0),
                mean_diff=mean,
                sd=sd,
                regression_e_value=tv.e_value,
                regression_max_e_value=tv.max_e_value,
                improvement_e_value=improve[t].e_value,
                ci_low=tv.ci_low,
                ci_high=tv.ci_high,
                alpha_share=ebh_alpha / k,
                e_threshold=k / ebh_alpha,
                regressed=t in reg_flags,
                improved=t in imp_flags,
            )
        )
    return EValueEvidence(
        method=METHOD if stratified_share is None else METHOD_VR,
        alpha=alpha,
        alpha_certify=alpha_certify,
        n_cases=len(pairs),
        family=tuple(present),
        untouched_types=len(by_type) - k,
        types=tuple(lines),
        regressed=tuple(t for t in present if t in reg_flags),
        improved=tuple(t for t in present if t in imp_flags),
        regressed_fwer=sv.regressed_fwer,
        pooled_mean_diff=sv.mean_diff,
        pooled_ci_low=sv.ci_low,
        pooled_ci_high=sv.ci_high,
        pooled_certify_e_value=sv.certify_e_value,
        pooled_regression_e_value=sv.pooled_regression_e_value,
        violations=(),
        stratified=strat_ev,
    )


def _money(minor: float) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}${abs(minor) / 100:,.2f}"


def summary_lines(ev: EValueEvidence) -> list[str]:
    """Plain-text lines for reports, PDFs and comments (one per tested case type)."""
    head = (
        f"{ev.method}; false-block FDR {ev.alpha:g}, certificate {ev.alpha_certify:g}; "
        f"{len(ev.family)} tested case type(s), {ev.untouched_types} untouched; "
        f"plan needs {ev.required_total_cases or 0} held-out cases, have {ev.n_cases}"
        + ("" if ev.sufficient else " (INSUFFICIENT EVIDENCE)")
    )
    out = [head]
    if ev.stratified is not None:
        st = ev.stratified
        out.append(
            f"stratified betting over the family at {st.alpha:g}: e-value {st.e_value:.3g} "
            f"(flags at {st.threshold:.3g}), " + ("REGRESSED" if st.rejected else "no call")
        )
    for t in ev.types:
        flag = "REGRESSED" if t.regressed else ("improved" if t.improved else "no call")
        out.append(
            f"{t.case_type}: n={t.n} (needs {t.required_n}), change {_money(t.mean_diff)}/case, "
            f"CS [{_money(t.ci_low)}, {_money(t.ci_high)}], e(worse)={t.regression_e_value:.3g}, "
            f"e(better)={t.improvement_e_value:.3g}, alpha share {t.alpha_share:.4g} "
            f"(e >= {t.e_threshold:.3g} alone), {flag}"
        )
    return out


def mdr_for(t: str, cap: float, cfg: GateConfig) -> float:
    o = cfg.evalue_type_min_detectable_regression_usd_minor
    if t in o:
        return o[t]
    return o.get(t.split("/", 1)[0], cfg.evalue_min_detectable_regression_fraction * cap)


class TypePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_type: str
    n: int
    required_n: int
    min_detectable_regression: float
    planning_sd: float


class SamplePlan(BaseModel):
    """Held-out size needed by the e-value gate (computed before outcomes)."""

    model_config = ConfigDict(frozen=True)

    method: str
    alpha: float
    beta: float
    types: tuple[TypePlan, ...]
    required_total_cases: int
    n_cases: int

    @property
    def insufficient(self) -> tuple[str, ...]:
        return tuple(t.case_type for t in self.types if t.n < t.required_n)


def plan_sample(
    counts: Mapping[str, int],
    caps: Mapping[str, float],
    tolerances: Mapping[str, float],
    family: Sequence[str],
    cfg: GateConfig,
) -> SamplePlan:
    """Per-type required cases from counts, caps and configuration only."""
    total = sum(counts.values())
    vr = cfg.stats == "evalue_vr"
    method = f"bernstein (scruple.stats.plan), alpha/K per type, power {1 - cfg.evalue_beta:g}"
    if vr:
        method = (
            f"bernstein, per type the smaller of stratified at w alpha and e-BH at "
            f"(1 - w) alpha / K (w = {cfg.evalue_vr_stratified_share:g}), "
            f"power {1 - cfg.evalue_beta:g}"
        )
    specs = []
    for t in sorted(family):
        cap, tol = caps[t], tolerances[t]
        specs.append(
            StratumSpec(
                name=t,
                frequency=counts[t] / total,
                cost_cap=cap,
                min_detectable_regression=min(mdr_for(t, cap, cfg), 0.999 * cap - tol),
                tolerance=tol,
                sd=cfg.evalue_planning_sd_fraction * cap,
            )
        )
    if not specs:
        return SamplePlan(
            method=method,
            alpha=cfg.evalue_alpha,
            beta=cfg.evalue_beta,
            types=(),
            required_total_cases=0,
            n_cases=total,
        )
    w = cfg.evalue_vr_stratified_share
    a_ebh = (1.0 - w) * cfg.evalue_alpha if vr else cfg.evalue_alpha
    p = plan(specs, alpha=a_ebh, beta=cfg.evalue_beta, method="bernstein")
    required = [sp.stratum_cases for sp in p.strata]
    if vr:
        required = [
            min(
                r,
                stratified_cases_needed(s, cfg.evalue_alpha, cfg.evalue_beta, share_of_alpha=w)[0],
            )
            for s, r in zip(specs, required, strict=True)
        ]
    return SamplePlan(
        method=method,
        alpha=cfg.evalue_alpha,
        beta=cfg.evalue_beta,
        types=tuple(
            TypePlan(
                case_type=s.name,
                n=counts[s.name],
                required_n=req,
                min_detectable_regression=s.min_detectable_regression,
                planning_sd=s.sd or 0.0,
            )
            for s, req in zip(specs, required, strict=True)
        ),
        required_total_cases=_total_for(specs, required, p.gamma) if vr else p.total_cases,
        n_cases=total,
    )


def _total_for(specs: Sequence[StratumSpec], required: Sequence[int], gamma: float) -> int:
    return max(
        total_cases_for_count(r, s.frequency, gamma) for s, r in zip(specs, required, strict=True)
    )


def with_plan(ev: EValueEvidence, sp: SamplePlan) -> EValueEvidence:
    by = {t.case_type: t for t in sp.types}
    types = tuple(
        t.model_copy(
            update={
                "min_detectable_regression": by[t.case_type].min_detectable_regression,
                "planning_sd": by[t.case_type].planning_sd,
                "required_n": by[t.case_type].required_n,
                "sd_within_assumption": t.sd <= by[t.case_type].planning_sd,
            }
        )
        if t.case_type in by
        else t
        for t in ev.types
    )
    return ev.model_copy(
        update={
            "types": types,
            "beta": sp.beta,
            "plan_method": sp.method,
            "required_total_cases": sp.required_total_cases,
            "sufficient": not sp.insufficient,
            "insufficient": sp.insufficient,
        }
    )


def _empty(n: int, cfg: GateConfig, violations: list[str]) -> EValueEvidence:
    return EValueEvidence(
        method=METHOD_VR if cfg.stats == "evalue_vr" else METHOD,
        alpha=cfg.evalue_alpha,
        alpha_certify=cfg.evalue_alpha_certify,
        n_cases=n,
        family=(),
        untouched_types=0,
        types=(),
        regressed=(),
        improved=(),
        regressed_fwer=(),
        pooled_mean_diff=0.0,
        pooled_ci_low=0.0,
        pooled_ci_high=0.0,
        pooled_certify_e_value=1.0,
        pooled_regression_e_value=1.0,
        violations=tuple(violations[:10]),
    )


def evaluate_evalue(
    cases: Sequence[DisputeCase], b: OutcomeScores, c: OutcomeScores, cfg: GateConfig
) -> EValueEvidence:
    """Shadow replay scores (baseline ``b``, candidate ``c``) in, evidence out."""
    pairs: list[LossPair] = []
    caps: dict[str, float] = {}
    counts: dict[str, int] = defaultdict(int)
    changed: dict[str, int] = defaultdict(int)
    violations: list[str] = []
    for case in cases:
        i = case.case_id
        t, cap = case_type(case, cfg)
        caps[t] = cap
        counts[t] += 1
        d = c.regret[i] - b.regret[i]
        if abs(d) > cap:
            violations.append(f"{i}: |loss change| {abs(d)} exceeds declared cap {cap:g} ({t})")
        if b.action[i] is not c.action[i]:
            changed[t] += 1
        elif d != 0:
            violations.append(f"{i}: loss changed although the decision did not ({t})")
        pairs.append(
            LossPair(case_id=i, case_type=t, baseline_loss=b.regret[i], candidate_loss=c.regret[i])
        )
    if violations:
        return _empty(len(cases), cfg, violations)
    tolerances = {t: tolerance_for(t, cfg) for t in caps}
    for t, tol in tolerances.items():
        if tol >= caps[t]:
            raise ValueError(f"tolerance for {t!r} must be below its cap {caps[t]:g}")
    sample = plan_sample(counts, caps, tolerances, sorted(changed), cfg)  # no outcomes
    ev = stratified_test(
        pairs,
        caps,
        tolerances,
        sorted(changed),
        alpha=cfg.evalue_alpha,
        alpha_certify=cfg.evalue_alpha_certify,
        confidence=cfg.confidence,
        cost_cap=cfg.evalue_cost_cap_usd_minor,
        changed=changed,
        stratified_share=cfg.evalue_vr_stratified_share if cfg.stats == "evalue_vr" else None,
    )
    return with_plan(ev, sample)
