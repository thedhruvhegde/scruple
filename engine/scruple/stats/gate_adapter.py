"""Pure adapter from per-case loss pairs to a structured statistical verdict.

The gate engine (``scruple/gate/engine.py``) is owned elsewhere; this module does not
import or change it. Wiring is documented in docs/math/06_gate_adapter.md. Summary:
build one ``LossPair`` per held-out case from ``score_outcomes`` regrets (baseline
and candidate) with ``case_type = scenario``, declare a cost cap, and call
``evaluate_pairs``. The verdict is advisory unless the engine chooses to enforce it.

Decision logic (each step is a result in docs/math/):

1. Block family (time-uniform FWER <= ``alpha_block``, any dependence): one betting
   e-process per case type for ``H0_k: mean_k <= t_k`` plus one pooled e-process for
   ``H0: mean <= 0``, combined by weighted e-Bonferroni on running maxima. Any
   rejection means "this case type (or the whole) regressed": BLOCK.
2. Certificate: pooled e-process for ``H0: mean >= tolerance``; certified when it ever
   reaches ``1/alpha_certify`` (Ville). P(certify | harmful) <= ``alpha_certify``.
3. Bayes (optional prior): composed rule; promote only if certified AND the
   cost-weighted Bayes rule agrees (Proposition 2 in 04_asymmetric_decision.md).
4. Otherwise HOLD.

e-BH and closed-testing rejections over the case types are reported too (informative,
evaluated at the end of the batch, which is a stopping time).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .decision import ComposedDecision, LossModel, NormalPrior, composed_decision, mean_and_se
from .decision import normal_posterior as _posterior
from .sequential import BettingConfidenceSequence, BettingEProcess
from .stratified import e_bh, e_bonferroni, e_closed_mean

POOLED = "__pooled__"


class LossPair(BaseModel):
    """One held-out case: losses under baseline and candidate (USD minor units)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    case_type: str
    baseline_loss: float
    candidate_loss: float

    @property
    def diff(self) -> float:
        return self.candidate_loss - self.baseline_loss


class StatsGateConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_cap: float = Field(gt=0.0)  # declared bound on |candidate - baseline| per case
    type_caps: dict[str, float] = Field(default_factory=dict)  # tighter per-type caps
    tolerance: float = 0.0  # certificate: promote only if mean diff < tolerance
    type_tolerances: dict[str, float] = Field(default_factory=dict)  # block if mean_k > t_k
    type_weights: dict[str, float] = Field(default_factory=dict)  # e-Bonferroni weights
    pooled_weight: float = Field(default=1.0, gt=0.0)
    alpha_block: float = Field(default=0.05, gt=0.0, lt=1.0)
    alpha_certify: float = Field(default=0.05, gt=0.0, lt=1.0)
    alpha_fdr: float = Field(default=0.1, gt=0.0, lt=1.0)
    prior: NormalPrior | None = None  # on the mean per-case diff, USD minor
    loss: LossModel = LossModel(wrong_promote=100.0, wrong_block=1.0)
    shuffle_seed: int | None = None  # seeded permutation of case order (exchangeable data)

    @model_validator(mode="after")
    def _caps(self) -> StatsGateConfig:
        for k, c in self.type_caps.items():
            if not 0 < c <= self.cost_cap:
                raise ValueError(f"type cap for {k!r} must lie in (0, cost_cap]")
        for k, t in self.type_tolerances.items():
            if abs(t) >= self.type_caps.get(k, self.cost_cap):
                raise ValueError(f"tolerance for {k!r} must be below its cap")
        if abs(self.tolerance) >= self.cost_cap:
            raise ValueError("tolerance must be below the cost cap")
        return self


class TypeVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_type: str
    n: int
    mean_diff: float  # USD minor per case (candidate - baseline)
    cap: float
    e_value: float  # regression e-process at the end of the batch
    max_e_value: float
    ci_low: float  # time-uniform CS at alpha_block, USD minor
    ci_high: float
    regressed: bool  # e-Bonferroni (FWER)


class StatsVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["promote", "hold", "block"]
    reasons: tuple[str, ...]
    n_cases: int
    mean_diff: float
    ci_low: float  # pooled time-uniform CS at alpha_certify, USD minor
    ci_high: float
    certify_e_value: float  # max over time, H0: mean >= tolerance
    certified: bool
    pooled_regression_e_value: float  # max over time, H0: mean <= 0
    types: tuple[TypeVerdict, ...]
    regressed_fwer: tuple[str, ...]
    regressed_fdr: tuple[str, ...]  # e-BH at alpha_fdr (current values)
    regressed_closed: tuple[str, ...]  # closed mean testing at alpha_block (current values)
    decision: ComposedDecision | None


def evaluate_pairs(pairs: Sequence[LossPair], cfg: StatsGateConfig) -> StatsVerdict:
    """Pure function: per-case loss pairs plus case types in, verdict out."""
    if not pairs:
        raise ValueError("no loss pairs")
    if len({p.case_id for p in pairs}) != len(pairs):
        raise ValueError("duplicate case_id")
    order = list(pairs)
    if cfg.shuffle_seed is not None:
        random.Random(cfg.shuffle_seed).shuffle(order)
    types = sorted({p.case_type for p in order})

    def cap(t: str) -> float:
        return cfg.type_caps.get(t, cfg.cost_cap)

    for p in order:
        if abs(p.diff) > cap(p.case_type) + 1e-9:
            raise ValueError(
                f"case {p.case_id}: |diff| {abs(p.diff)} exceeds cap {cap(p.case_type)}"
            )

    procs = {t: BettingEProcess(cfg.type_tolerances.get(t, 0.0) / cap(t), "greater") for t in types}
    css = {t: BettingConfidenceSequence(cfg.alpha_block) for t in types}
    pooled_reg = BettingEProcess(0.0, "greater")
    certify = BettingEProcess(cfg.tolerance / cfg.cost_cap, "less")
    pooled_cs = BettingConfidenceSequence(cfg.alpha_certify)
    by_type: dict[str, list[float]] = defaultdict(list)
    for p in order:
        x_t = max(-1.0, min(1.0, p.diff / cap(p.case_type)))
        x = max(-1.0, min(1.0, p.diff / cfg.cost_cap))
        procs[p.case_type].update(x_t)
        css[p.case_type].update(x_t)
        pooled_reg.update(x)
        certify.update(x)
        pooled_cs.update(x)
        by_type[p.case_type].append(p.diff)

    # 1. block family: K types + pooled, weighted e-Bonferroni on running maxima
    names = [*types, POOLED]
    weights = [cfg.type_weights.get(t, 1.0) for t in types] + [cfg.pooled_weight]
    maxima = [procs[t].max_e_value for t in types] + [pooled_reg.max_e_value]
    flagged = {names[i] for i in e_bonferroni(maxima, cfg.alpha_block, weights)}
    regressed_fwer = tuple(t for t in names if t in flagged)

    current = [procs[t].e_value for t in types]
    regressed_fdr = tuple(types[i] for i in e_bh(current, cfg.alpha_fdr))
    regressed_closed = tuple(types[i] for i in e_closed_mean(current, cfg.alpha_block))

    type_verdicts = []
    for t in types:
        lo, hi = css[t].interval()
        diffs = by_type[t]
        type_verdicts.append(
            TypeVerdict(
                case_type=t,
                n=len(diffs),
                mean_diff=math.fsum(diffs) / len(diffs),
                cap=cap(t),
                e_value=procs[t].e_value,
                max_e_value=procs[t].max_e_value,
                ci_low=lo * cap(t),
                ci_high=hi * cap(t),
                regressed=t in flagged,
            )
        )

    # 2. certificate, 3. composed Bayes rule
    all_diffs = [p.diff for p in order]
    mean = math.fsum(all_diffs) / len(all_diffs)
    certified = certify.rejected(cfg.alpha_certify)
    decision = None
    bayes_ok = True
    if cfg.prior is not None and len(all_diffs) >= 2:
        est, se = mean_and_se(all_diffs)
        decision = composed_decision(
            certify.max_e_value,
            cfg.alpha_certify,
            _posterior(cfg.prior, est, se),
            cfg.tolerance,
            cfg.loss,
        )
        bayes_ok = decision.bayes_promote

    lo, hi = pooled_cs.interval()
    reasons: list[str] = []
    if regressed_fwer:
        outcome: Literal["promote", "hold", "block"] = "block"
        reasons.append(
            f"regression detected (FWER <= {cfg.alpha_block}): {', '.join(regressed_fwer)}"
        )
    elif certified and bayes_ok:
        outcome = "promote"
        reasons.append(
            f"certified mean diff < {cfg.tolerance:g} (e-value {certify.max_e_value:.3g} >= "
            f"{1 / cfg.alpha_certify:g})"
        )
        if decision is not None:
            reasons.append(f"Bayes rule agrees: P(harm) = {decision.prob_harm:.4f}")
    else:
        outcome = "hold"
        if not certified:
            reasons.append(
                f"not certified: e-value {certify.max_e_value:.3g} < {1 / cfg.alpha_certify:g}"
            )
        if not bayes_ok and decision is not None:
            reasons.append(
                f"Bayes rule blocks: P(harm) = {decision.prob_harm:.4f}, "
                f"risk promote {decision.risk_promote:.3g} > block {decision.risk_block:.3g}"
            )
    return StatsVerdict(
        outcome=outcome,
        reasons=tuple(reasons),
        n_cases=len(order),
        mean_diff=mean,
        ci_low=lo * cfg.cost_cap,
        ci_high=hi * cfg.cost_cap,
        certify_e_value=certify.max_e_value,
        certified=certified,
        pooled_regression_e_value=pooled_reg.max_e_value,
        types=tuple(type_verdicts),
        regressed_fwer=regressed_fwer,
        regressed_fdr=regressed_fdr,
        regressed_closed=regressed_closed,
        decision=decision,
    )
