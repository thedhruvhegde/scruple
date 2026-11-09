"""Replay historical cases against a candidate and say whether it would have done better.

For every resolved chargeback in the history (in one currency):

1. Build the decision-time view (`scruple.backtest.view`; no look-ahead) and ask the
   candidate. `None` keeps the historical action.
2. Baseline cost: what actually happened, from the case's ledger postings (merchant
   net outflow: debits plus fees minus reinstatements). Without postings (Adyen, CSV
   without a ledger) the cost is read from the state (amount if lost or accepted, else
   0) and the report says so.
3. Candidate cost:
   - same action as history: **observed** (equal to the baseline);
   - accept where history contested: **estimated**, the chargeback stands: amount plus
     the fees posted by the decision time (ASSUMPTION: those fees are owed whatever the
     merchant does);
   - contest where history accepted: **estimated** from the calibrated simulator:
     fees by decision time + `contest_fee_minor` + (1 - p_win) * amount, with p_win the
     profile's win rate for the reason. A band uses the ends of its 95% Wilson
     interval. Selection bias: the rate comes from cases merchants chose to contest;
   - escalate: **estimated**, the historical outcome plus `escalation_cost_minor`
     (ASSUMPTION: the analyst reaches the decision that was actually made).
4. Statistics (`scruple.stats` through `scruple.stats.gate_adapter.evaluate_pairs`):
   per-case pairs (baseline, candidate) with the reason as case type, caps from amounts
   and fees. Blocking uses the adapter's weighted e-Bonferroni (FWER); per-reason
   improvement uses one betting e-process per reason combined by e-BH (FDR). Planning
   (`scruple.stats.plan`) reports how many cases each reason would need.

Verdicts: `better`, `worse`, `no change` (the candidate never differs from history),
or `insufficient evidence`. The same statistics are re-run with the pessimistic and the
optimistic end of every estimate; if the verdict moves, the report says so.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.backtest.candidate import Candidate
from scruple.backtest.history import FEE_ACCOUNT, HistoricalCase, History
from scruple.backtest.view import DEFAULTED_RULE_FIELDS, PaymentIndex, build_view
from scruple.calibration import (
    CalibrationConfig,
    CalibrationProfile,
    CalibrationReport,
    calibrate,
)
from scruple.calibration.facts import contested, is_inquiry_level, is_resolved_chargeback
from scruple.domain import Action
from scruple.ingest.models import CaseState
from scruple.sim.models import ESCALATION_COST_USD
from scruple.stats import (
    BettingEProcess,
    LossPair,
    StatsGateConfig,
    StatsVerdict,
    StratumSpec,
    e_bh,
    evaluate_pairs,
    plan,
)

Basis = Literal["observed", "estimated"]
Method = Literal[
    "observed",
    "accept_chargeback_stands",
    "contest_simulated_win_rate",
    "escalate_historical_plus_cost",
]
Verdict = Literal["better", "worse", "no change", "insufficient evidence"]

ASSUMPTIONS = (
    "Exports do not timestamp evidence submission or the outcome; both are treated as "
    "unknown at decision time (the decision is made when the dispute opens).",
    "Fees posted by the decision time are owed whatever the merchant does.",
    "Contest estimates use the calibrated win rate of the reason; that rate comes from "
    "cases merchants chose to contest (selection bias: likely optimistic for cases "
    "they did not).",
    "Escalation: the analyst reaches the decision that was actually made, at "
    "escalation_cost_minor (synthetic default from scruple.sim.models).",
    "Rule fields an export does not carry use fixed defaults: "
    + ", ".join(f"{k}={v}" for k, v in DEFAULTED_RULE_FIELDS.items())
    + " (mcc, delivery_confirmed, three_ds and has_prior_refund are read from the "
    "payment facts when the export has them; rule_fields_defaulted counts the cases "
    "where a default was used).",
)


class BacktestConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int = Field(default=10, ge=2)  # calibration suppression threshold
    currency: str | None = None  # None: the history's most common currency
    decision_delay_hours: float = Field(default=0.0, ge=0.0, le=24 * 365)
    escalation_cost_minor: int = Field(default=ESCALATION_COST_USD, ge=0)
    contest_fee_minor: int = Field(default=0, ge=0)
    alpha_block: float = Field(default=0.05, gt=0, lt=1)
    alpha_certify: float = Field(default=0.05, gt=0, lt=1)
    alpha_fdr: float = Field(default=0.1, gt=0, lt=1)
    mde_fraction: float = Field(default=0.05, gt=0, lt=1)  # planning: regression / cap
    beta: float = Field(default=0.2, gt=0, lt=1)
    shuffle_seed: int | None = None  # None: cases in time order
    calibration_seed: int = 0
    n_synthetic: int = Field(default=5_000, ge=100, le=200_000)
    include_cases: bool = False


class CaseLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    reason: str
    opened_at: datetime
    historical_action: Action
    candidate_action: Action
    decided_by: str
    basis: Basis
    method: Method
    cost_source: Literal["ledger", "state"]
    baseline_cost: float  # minor units of the backtest currency
    candidate_cost: float
    candidate_cost_low: float  # optimistic end of the estimate
    candidate_cost_high: float  # pessimistic end
    cap: float
    defaulted: tuple[str, ...] = ()  # rule fields filled with a default for this case


class StatsRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["promote", "hold", "block"]
    mean_diff: float
    ci_low: float
    ci_high: float


class ReasonPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    cases: int
    cases_needed: int | None  # None: planning not possible (see note)
    sufficient: bool
    note: str = ""


class ReasonResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: str
    n: int
    n_observed: int
    n_estimated: int
    baseline_cost: float
    candidate_cost: float
    candidate_cost_observed: float
    candidate_cost_estimated: float
    candidate_cost_low: float
    candidate_cost_high: float
    mean_diff: float
    ci_low: float
    ci_high: float
    regression_e_value: float
    improvement_e_value: float
    worse_fwer: bool
    better_fdr: bool
    worse_fdr: bool
    verdict: Verdict
    plan: ReasonPlan


class BacktestReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset: str
    synthetic: bool
    format: str
    candidate: str
    currency: str
    as_of: datetime | None  # latest case open time: the report uses no wall clock
    config: BacktestConfig
    n_input: int
    n_included: int
    excluded: dict[str, int]
    skipped_input: dict[str, int]
    n_observed: int
    n_estimated: int
    cost_source: dict[str, int]
    # rule field -> included cases where the export lacked it and a default was used
    rule_fields_defaulted: dict[str, int] = {}
    ledger_merchant_net: int  # merchant balance over all postings in the currency
    ledger_reconciled: bool  # observed costs of every case == -ledger_merchant_net
    baseline_cost: float
    candidate_cost: float
    candidate_cost_observed: float
    candidate_cost_estimated: float
    candidate_cost_low: float
    candidate_cost_high: float
    verdict: Verdict
    robust: bool  # verdict unchanged at both ends of the estimate bands
    reasons_text: tuple[str, ...]
    stats: StatsVerdict | None
    pessimistic: StatsRun | None
    optimistic: StatsRun | None
    by_reason: tuple[ReasonResult, ...]
    planning_note: str
    calibration: CalibrationReport | None
    assumptions: tuple[str, ...] = ASSUMPTIONS
    cases: tuple[CaseLine, ...] = ()


# --- costs --------------------------------------------------------------------------


def _merchant_net(hc: HistoricalCase, account: str) -> int:
    return sum(ln.amount_minor for t in hc.ledger for ln in t.lines if ln.account == account)


def _fees(hc: HistoricalCase, until: datetime | None = None) -> int:
    return sum(
        ln.amount_minor
        for t in hc.ledger
        if until is None or t.occurred_at <= until
        for ln in t.lines
        if ln.account == FEE_ACCOUNT and ln.amount_minor > 0
    )


class Priced(BaseModel):
    """The cost of one action on one case, and how it was obtained."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: Basis
    method: Method
    cost_source: Literal["ledger", "state"]
    baseline: float  # realised cost of what was actually done
    cost: float
    low: float  # optimistic end
    high: float  # pessimistic end


def realised_cost(hc: HistoricalCase, merchant_account: str) -> tuple[float, str]:
    """What the case actually cost the merchant: ledger postings when there are any,
    else the amount if it ended lost or accepted (and the source used)."""
    if hc.ledger:
        return float(-_merchant_net(hc, merchant_account)), "ledger"
    c = hc.case
    return float(c.amount_minor if c.state in (CaseState.LOST, CaseState.ACCEPTED) else 0), "state"


def price_action(
    hc: HistoricalCase,
    historical: Action,
    action: Action,
    at: datetime,
    profile: CalibrationProfile,
    cfg: BacktestConfig,
    merchant_account: str = "merchant",
) -> Priced:
    """Observed cost if `action` is what was done, else the estimate described above."""
    c = hc.case
    baseline, source = realised_cost(hc, merchant_account)
    fee_now = _fees(hc, at)
    amount = c.amount_minor
    method: Method
    if action is historical:
        basis: Basis = "observed"
        method = "observed"
        cost = lo = hi = baseline
    elif action is Action.ESCALATE:
        basis, method = "estimated", "escalate_historical_plus_cost"
        cost = lo = hi = baseline + cfg.escalation_cost_minor
    elif action is Action.ACCEPT:
        basis, method = "estimated", "accept_chargeback_stands"
        cost = lo = hi = float(amount + fee_now)
    else:
        basis, method = "estimated", "contest_simulated_win_rate"
        rp = profile.reason(c.source_reason)
        p = profile.win_probability(c.source_reason)
        p_lo, p_hi = rp.win_interval()
        base = fee_now + cfg.contest_fee_minor
        cost = base + (1 - p) * amount
        lo, hi = base + (1 - p_hi) * amount, base + (1 - p_lo) * amount
    return Priced(
        basis=basis,
        method=method,
        cost_source="ledger" if source == "ledger" else "state",
        baseline=baseline,
        cost=cost,
        low=lo,
        high=hi,
    )


def _line(
    hc: HistoricalCase,
    history: History,
    candidate: Candidate,
    profile: CalibrationProfile,
    index: PaymentIndex,
    cfg: BacktestConfig,
) -> CaseLine:
    c = hc.case
    at = c.opened_at + timedelta(hours=cfg.decision_delay_hours)
    view = build_view(hc, index, at, history.merchant_account)
    decision = candidate.decide(view)
    hist = Action.CONTEST if contested(c) else Action.ACCEPT
    action = decision.action or hist
    pr = price_action(hc, hist, action, at, profile, cfg, history.merchant_account)
    baseline = pr.baseline
    cap = float(c.amount_minor + _fees(hc) + cfg.contest_fee_minor + cfg.escalation_cost_minor)
    cap = max(cap, 1.0, *(abs(x - baseline) for x in (pr.cost, pr.low, pr.high)))
    return CaseLine(
        case_id=c.case_id,
        reason=c.source_reason,
        opened_at=c.opened_at,
        historical_action=hist,
        candidate_action=action,
        decided_by=decision.ref,
        basis=pr.basis,
        method=pr.method,
        cost_source=pr.cost_source,
        baseline_cost=baseline,
        candidate_cost=pr.cost,
        candidate_cost_low=pr.low,
        candidate_cost_high=pr.high,
        cap=cap,
        defaulted=view.defaulted_rule_fields(),
    )


# --- statistics ----------------------------------------------------------------------


def _pairs(lines: Sequence[CaseLine], which: str) -> list[LossPair]:
    return [
        LossPair(
            case_id=x.case_id,
            case_type=x.reason,
            baseline_loss=x.baseline_cost,
            candidate_loss=getattr(x, which),
        )
        for x in lines
    ]


def _stats_config(lines: Sequence[CaseLine], cfg: BacktestConfig) -> StatsGateConfig:
    caps: dict[str, float] = defaultdict(float)
    for x in lines:
        caps[x.reason] = max(caps[x.reason], x.cap)
    return StatsGateConfig(
        cost_cap=max(caps.values()),
        type_caps=dict(caps),
        alpha_block=cfg.alpha_block,
        alpha_certify=cfg.alpha_certify,
        alpha_fdr=cfg.alpha_fdr,
        shuffle_seed=cfg.shuffle_seed,
    )


def _improvement(lines: Sequence[CaseLine], scfg: StatsGateConfig) -> dict[str, BettingEProcess]:
    procs: dict[str, BettingEProcess] = {}
    for x in lines:
        cap = scfg.type_caps.get(x.reason, scfg.cost_cap)
        proc = procs.setdefault(x.reason, BettingEProcess(0.0, "less"))
        proc.update(max(-1.0, min(1.0, (x.candidate_cost - x.baseline_cost) / cap)))
    return procs


def _run(lines: Sequence[CaseLine], which: str, scfg: StatsGateConfig) -> StatsRun:
    v = evaluate_pairs(_pairs(lines, which), scfg)
    return StatsRun(outcome=v.outcome, mean_diff=v.mean_diff, ci_low=v.ci_low, ci_high=v.ci_high)


def _verdict_of(outcome: str, changed: bool) -> Verdict:
    if not changed:
        return "no change"
    if outcome == "promote":
        return "better"
    if outcome == "block":
        return "worse"
    return "insufficient evidence"


def _plans(
    lines: Sequence[CaseLine], scfg: StatsGateConfig, cfg: BacktestConfig
) -> tuple[dict[str, ReasonPlan], str]:
    n = len(lines)
    by: dict[str, list[float]] = defaultdict(list)
    for x in lines:
        by[x.reason].append(x.candidate_cost - x.baseline_cost)
    specs = []
    for r in sorted(by):
        diffs = by[r]
        cap = scfg.type_caps.get(r, scfg.cost_cap)
        sd = statistics.pstdev(diffs) if len(diffs) > 1 else 0.0
        specs.append(
            StratumSpec(
                name=r,
                frequency=len(diffs) / n,
                cost_cap=cap,
                min_detectable_regression=cfg.mde_fraction * cap,
                sd=sd if sd > 0 else None,
            )
        )
    note = (
        f"Cases needed per reason to flag a regression of {cfg.mde_fraction:g} x the "
        f"reason's cap with power {1 - cfg.beta:g} at FWER {cfg.alpha_block:g} "
        "(scruple.stats.plan, Bernstein bound, sd taken from the observed differences)."
    )
    try:
        p = plan(specs, alpha=cfg.alpha_block, beta=cfg.beta)
    except ValueError as e:
        out = {
            s.name: ReasonPlan(
                reason=s.name,
                cases=len(by[s.name]),
                cases_needed=None,
                sufficient=False,
                note=str(e),
            )
            for s in specs
        }
        return out, f"planning not possible: {e}"
    return {
        sp.name: ReasonPlan(
            reason=sp.name,
            cases=len(by[sp.name]),
            cases_needed=sp.stratum_cases,
            sufficient=len(by[sp.name]) >= sp.stratum_cases,
        )
        for sp in p.strata
    }, note


# --- the run ---------------------------------------------------------------------------


def run_backtest(
    history: History,
    candidate: Candidate,
    *,
    config: BacktestConfig | None = None,
    profile: CalibrationProfile | None = None,
) -> BacktestReport:
    """Pure: the same history, candidate, profile and config give the same report.

    Without `profile` one is fitted from the history's aggregates (153) and the
    calibration report is attached."""
    cfg = config or BacktestConfig()
    counts = Counter(hc.case.currency for hc in history.cases)
    currency = cfg.currency or (min(counts, key=lambda x: (-counts[x], x)) if counts else "USD")
    calib: CalibrationReport | None = None
    if profile is None:
        calib = calibrate(
            [hc.case for hc in history.cases],
            history.movements,
            CalibrationConfig(k=cfg.k),
            n_synthetic=cfg.n_synthetic,
            seed=cfg.calibration_seed,
        )
        profile = calib.profile

    excluded: Counter[str] = Counter()
    included: list[HistoricalCase] = []
    for hc in history.cases:
        c = hc.case
        if c.currency != currency:
            excluded[f"currency other than {currency}"] += 1
        elif is_inquiry_level(c):
            excluded["inquiry or prevented (no chargeback decision)"] += 1
        elif not is_resolved_chargeback(c):
            excluded["open (no outcome yet)"] += 1
        else:
            included.append(hc)
    index = PaymentIndex(history.cases)
    lines = [_line(hc, history, candidate, profile, index, cfg) for hc in included]

    led = history.ledger()
    in_ccy = [hc for hc in history.cases if hc.case.currency == currency and hc.ledger]
    observed_total = sum(-_merchant_net(hc, history.merchant_account) for hc in in_ccy)
    ledger_net = led.balance(history.merchant_account, currency)
    as_of = max((hc.case.opened_at for hc in history.cases), default=None)

    def total(attr: str, rows: Sequence[CaseLine] = lines) -> float:
        return round(math.fsum(getattr(x, attr) for x in rows), 2)

    observed = [x for x in lines if x.basis == "observed"]
    estimated = [x for x in lines if x.basis == "estimated"]
    changed = bool(estimated)
    stats = pess = opt = None
    by_reason: list[ReasonResult] = []
    planning_note = "no included cases"
    verdict: Verdict = "insufficient evidence"
    robust = False
    reasons_text: tuple[str, ...] = ("no resolved chargebacks in the history",)
    if lines:
        scfg = _stats_config(lines, cfg)
        stats = evaluate_pairs(_pairs(lines, "candidate_cost"), scfg)
        pess = _run(lines, "candidate_cost_high", scfg)
        opt = _run(lines, "candidate_cost_low", scfg)
        verdict = _verdict_of(stats.outcome, changed)
        robust = pess.outcome == stats.outcome == opt.outcome
        reasons_text = stats.reasons
        procs = _improvement(lines, scfg)
        names = sorted(procs)
        better = {names[i] for i in e_bh([procs[r].e_value for r in names], cfg.alpha_fdr)}
        plans, planning_note = _plans(lines, scfg, cfg)
        types = {t.case_type: t for t in stats.types}
        for r in names:
            rows = [x for x in lines if x.reason == r]
            t = types[r]
            r_changed = any(x.basis == "estimated" for x in rows)
            rv: Verdict
            if not r_changed:
                rv = "no change"
            elif t.regressed:
                rv = "worse"
            elif r in better:
                rv = "better"
            else:
                rv = "insufficient evidence"
            obs = [x for x in rows if x.basis == "observed"]
            est = [x for x in rows if x.basis == "estimated"]
            by_reason.append(
                ReasonResult(
                    reason=r,
                    n=len(rows),
                    n_observed=len(obs),
                    n_estimated=len(est),
                    baseline_cost=total("baseline_cost", rows),
                    candidate_cost=total("candidate_cost", rows),
                    candidate_cost_observed=total("candidate_cost", obs),
                    candidate_cost_estimated=total("candidate_cost", est),
                    candidate_cost_low=total("candidate_cost_low", rows),
                    candidate_cost_high=total("candidate_cost_high", rows),
                    mean_diff=t.mean_diff,
                    ci_low=t.ci_low,
                    ci_high=t.ci_high,
                    regression_e_value=t.e_value,
                    improvement_e_value=procs[r].e_value,
                    worse_fwer=t.regressed,
                    better_fdr=r in better,
                    worse_fdr=r in stats.regressed_fdr,
                    verdict=rv,
                    plan=plans[r],
                )
            )

    return BacktestReport(
        dataset=history.label,
        synthetic=history.synthetic,
        format=history.format,
        candidate=candidate.name,
        currency=currency,
        as_of=as_of,
        config=cfg,
        n_input=len(history.cases),
        n_included=len(lines),
        excluded=dict(sorted(excluded.items())),
        skipped_input=history.skipped,
        n_observed=len(observed),
        n_estimated=len(estimated),
        cost_source=dict(sorted(Counter(x.cost_source for x in lines).items())),
        rule_fields_defaulted=dict(sorted(Counter(f for x in lines for f in x.defaulted).items())),
        ledger_merchant_net=ledger_net,
        ledger_reconciled=observed_total == -ledger_net,
        baseline_cost=total("baseline_cost"),
        candidate_cost=total("candidate_cost"),
        candidate_cost_observed=total("candidate_cost", observed),
        candidate_cost_estimated=total("candidate_cost", estimated),
        candidate_cost_low=total("candidate_cost_low"),
        candidate_cost_high=total("candidate_cost_high"),
        verdict=verdict,
        robust=robust,
        reasons_text=reasons_text,
        stats=stats,
        pessimistic=pess,
        optimistic=opt,
        by_reason=tuple(by_reason),
        planning_note=planning_note,
        calibration=calib,
        cases=tuple(lines) if cfg.include_cases else (),
    )
