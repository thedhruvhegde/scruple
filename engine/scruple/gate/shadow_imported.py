"""Shadow mode on imported cases: real decisions, no simulator, no ground truth.

The production agent (or an export of what it did) sends each decision it made on a
real case: the `ExternalCase`, the action, when it decided, and the ledger postings
known so far. The recorder asks the candidate what it would have done from the
decision-time view (`scruple.backtest.view`, no look-ahead) and scores only what can
be observed. Outcomes arrive later (`observe`), when the partner re-exports resolved
cases; costs are scored then.

Every metric carries a basis:

- **observed**: computed from the decisions and the case and ledger records alone.
  Agreement, changed decisions, the checks below, the realised cost of what
  production did, and the candidate's cost where it did the same thing.
- **estimated**: the candidate acted differently, so what would have happened is not
  in any record. Priced exactly as the backtest prices it
  (`scruple.backtest.engine.price_action`: calibrated win rate for a contest, the
  chargeback standing for an accept, the analyst's cost for an escalation), with an
  optimistic and a pessimistic end.

Checks (all read observable fields only; counted per decision, by name):

- process `decided_before_open`: a decision timestamped before the case opened.
- deadline `contest_after_deadline`: a contest when, at decision time, the evidence
  due date had passed or the case allowed no response. It mirrors the synthetic
  `no_contest_without_timely_evidence` constraint (docs/ASSUMPTIONS.md A-6); the
  partner's own constraints are added as `extra_checks` (kind "partner"). Deadline
  and partner checks run on production's action and on the candidate's.
- evidence (once the outcome is in) `contest_without_evidence`: production contested
  but the resolved case shows no evidence submitted; `accept_but_evidence_submitted`:
  the recorded action and what happened disagree.
- ledger `ledger_wrong_case` / `ledger_currency_mismatch` / `ledger_conflict` /
  `ledger_exceeds_dispute`: a posting names another case, is in another currency,
  conflicts with a posting of the same id, or the withdrawals exceed the dispute
  amount.

Zero side effects, as for `ShadowRecorder`: this module imports nothing that can write
(no storage, registry, audit log, service or file output; a test checks the import
graph), keeps immutable inputs and in-memory counters only, and `record` returns
statistics, never an action to execute.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from scruple.backtest.candidate import Candidate
from scruple.backtest.engine import BacktestConfig, price_action
from scruple.backtest.history import MERCHANT_ACCOUNT, HistoricalCase
from scruple.backtest.view import DecisionView, PaymentIndex, build_view
from scruple.calibration.facts import is_resolved_chargeback
from scruple.calibration.profile import DEFAULT_PROFILE, CalibrationProfile
from scruple.domain import Action
from scruple.ingest.errors import IngestError
from scruple.ingest.ledger import InMemoryLedger, LedgerTransaction, from_movement
from scruple.ingest.models import ExternalCase, LedgerMovement

MAX_BATCH = 10_000
Basis = Literal["observed", "estimated"]
Kind = Literal["process", "deadline", "evidence", "ledger", "partner"]
CheckFn = Callable[[DecisionView, Action], bool]  # True: the action breaks the check


class ImportedDecision(BaseModel):
    """One decision production made on a real (imported) case. Input only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case: ExternalCase
    action: Action
    decided_at: AwareDatetime
    agent_id: str = Field(default="production", pattern=r"^[A-Za-z0-9._@:-]{1,128}$")
    movements: tuple[LedgerMovement, ...] = Field(default=(), max_length=64)


class ImportedOutcome(BaseModel):
    """A later version of a recorded case (re-exported once it resolved). Input only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case: ExternalCase
    movements: tuple[LedgerMovement, ...] = Field(default=(), max_length=64)


class ImportedDecisionBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: tuple[ImportedDecision, ...] = Field(min_length=1, max_length=MAX_BATCH)


class ImportedOutcomeBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: tuple[ImportedOutcome, ...] = Field(min_length=1, max_length=MAX_BATCH)


class Metric(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    basis: Basis
    value: float | None  # None: nothing to compute yet
    n: int  # decisions the value is computed over
    low: float | None = None  # optimistic end (estimated cost metrics)
    high: float | None = None  # pessimistic end
    note: str = ""


class FindingCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str
    kind: Kind
    basis: Basis = "observed"
    production: int
    candidate: int | None  # None: the check reads what happened, not the action


class ImportedBatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    recorded: int
    agree: int
    candidate_findings: int
    total_recorded: int


class ImportedObserveResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    updated: int
    resolved: int
    unknown: int  # outcomes for cases never recorded (ignored, counted)


class ImportedShadowSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: str  # "real (imported)" or "synthetic"
    candidate: str
    profile: str  # where estimated costs come from
    decisions: int
    resolved: int
    open: int
    currency: str | None  # cost metrics cover this currency only
    metrics: tuple[Metric, ...]
    findings: tuple[FindingCount, ...]
    by_reason: dict[str, dict[str, int]]  # reason -> {n, agree, changed, resolved}

    def metric(self, name: str) -> Metric:
        return next(m for m in self.metrics if m.name == name)


@dataclass
class _Rec:
    decision: ImportedDecision
    candidate: Action
    decided_by: str
    view_at: datetime
    case: ExternalCase
    movements: dict[str, LedgerMovement] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    candidate_findings: list[str] = field(default_factory=list)


KINDS: dict[str, Kind] = {
    "decided_before_open": "process",
    "contest_after_deadline": "deadline",
    "contest_without_evidence": "evidence",
    "accept_but_evidence_submitted": "evidence",
    "ledger_wrong_case": "ledger",
    "ledger_currency_mismatch": "ledger",
    "ledger_exceeds_dispute": "ledger",
    "ledger_conflict": "ledger",
}
ACTION_CHECKS = frozenset({"contest_after_deadline"})


def contest_after_deadline(view: DecisionView, action: Action) -> bool:
    return action is Action.CONTEST and not view.response_allowed


def ledger_findings(case: ExternalCase, moves: Sequence[LedgerMovement]) -> list[str]:
    out: list[str] = []
    led = InMemoryLedger()
    withdrawn = 0
    for m in moves:
        if m.case_external_id != case.external_id or m.source != case.source:
            out.append("ledger_wrong_case")
            continue
        if m.currency != case.currency:
            out.append("ledger_currency_mismatch")
        try:
            for t in from_movement(m, MERCHANT_ACCOUNT):
                led.post(t)
        except IngestError:
            out.append("ledger_conflict")
        if m.amount_minor < 0 and m.currency == case.currency:
            withdrawn -= m.amount_minor
    if withdrawn > case.amount_minor:
        out.append("ledger_exceeds_dispute")
    return out


def evidence_findings(case: ExternalCase, action: Action) -> list[str]:
    if not is_resolved_chargeback(case):
        return []
    if action is Action.CONTEST and not case.evidence_submitted:
        return ["contest_without_evidence"]
    if action is Action.ACCEPT and case.evidence_submitted:
        return ["accept_but_evidence_submitted"]
    return []


def _historical(case: ExternalCase, moves: Sequence[LedgerMovement]) -> HistoricalCase:
    """The case plus the postings that cleanly belong to it (others are findings)."""
    txns: dict[str, LedgerTransaction] = {}
    for m in moves:
        if m.case_external_id != case.external_id or m.currency != case.currency:
            continue
        for t in from_movement(m, MERCHANT_ACCOUNT):
            txns.setdefault(t.external_id, t)
    return HistoricalCase(
        case=case, ledger=tuple(sorted(txns.values(), key=lambda t: (t.occurred_at, t.external_id)))
    )


class ImportedShadowRecorder:
    """Scores imported decisions for one candidate. Holds only in-memory state."""

    def __init__(
        self,
        candidate: Candidate,
        *,
        profile: CalibrationProfile | None = None,
        config: BacktestConfig | None = None,
        extra_checks: Sequence[tuple[str, CheckFn]] = (),
        synthetic: bool = False,
    ) -> None:
        self.candidate = candidate
        self._profile = profile or DEFAULT_PROFILE
        self._profile_note = (
            "calibrated profile supplied by the caller"
            if profile is not None
            else "DEFAULT_PROFILE (synthetic, uncalibrated)"
        )
        self._cfg = config or BacktestConfig()
        clash = {n for n, _ in extra_checks} & set(KINDS)
        if clash:
            raise ValueError(f"check names already used: {sorted(clash)}")
        self._checks: tuple[tuple[str, CheckFn], ...] = (
            ("contest_after_deadline", contest_after_deadline),
            *extra_checks,
        )
        self._partner = frozenset(n for n, _ in extra_checks)
        self._synthetic = synthetic
        self._recs: dict[str, _Rec] = {}

    def _kind(self, check: str) -> Kind:
        return "partner" if check in self._partner else KINDS[check]

    # --- recording -----------------------------------------------------------------

    def record(self, decisions: Sequence[ImportedDecision]) -> ImportedBatchResult:
        """Score a batch. Rejects the whole batch if a case repeats or was recorded."""
        if not decisions:
            raise ValueError("empty batch")
        if len(decisions) > MAX_BATCH:
            raise ValueError(f"batch larger than {MAX_BATCH}")
        ids = [d.case.case_id for d in decisions]
        repeats = {i for i, c in Counter(ids).items() if c > 1}
        dup = repeats | {i for i in ids if i in self._recs}
        if dup:
            raise ValueError(f"already recorded: {', '.join(sorted(dup)[:5])}")
        hcs = [_historical(d.case, d.movements) for d in decisions]
        index = PaymentIndex(tuple(hcs))
        new: list[_Rec] = []
        for d, hc in zip(decisions, hcs, strict=True):
            at = max(d.decided_at, d.case.opened_at)
            view = build_view(hc, index, at, MERCHANT_ACCOUNT)
            out = self.candidate.decide(view)
            rec = _Rec(
                decision=d,
                candidate=out.action or d.action,
                decided_by=out.ref,
                view_at=at,
                case=d.case,
                movements={m.external_id: m for m in d.movements},
            )
            if d.decided_at < d.case.opened_at:
                rec.findings.append("decided_before_open")
            for name, fn in self._checks:
                if fn(view, d.action):
                    rec.findings.append(name)
                if fn(view, rec.candidate):
                    rec.candidate_findings.append(name)
            new.append(rec)
        for r in new:
            self._recs[r.case.case_id] = r
        return ImportedBatchResult(
            recorded=len(new),
            agree=sum(r.candidate is r.decision.action for r in new),
            candidate_findings=sum(len(r.candidate_findings) for r in new),
            total_recorded=len(self._recs),
        )

    def observe(self, outcomes: Sequence[ImportedOutcome]) -> ImportedObserveResult:
        """Attach later versions of recorded cases. Never changes a recorded decision."""
        updated = unknown = 0
        for o in outcomes:
            rec = self._recs.get(o.case.case_id)
            if rec is None:
                unknown += 1
                continue
            old = rec.case
            if (o.case.opened_at, o.case.amount_minor, o.case.currency) != (
                old.opened_at,
                old.amount_minor,
                old.currency,
            ):
                raise ValueError(f"{o.case.case_id}: outcome does not match the recorded case")
            rec.case = o.case
            for m in o.movements:
                rec.movements.setdefault(m.external_id, m)
            updated += 1
        resolved = sum(1 for r in self._recs.values() if is_resolved_chargeback(r.case))
        return ImportedObserveResult(updated=updated, resolved=resolved, unknown=unknown)

    # --- summary ---------------------------------------------------------------------

    def summary(self) -> ImportedShadowSummary:
        recs = [self._recs[k] for k in sorted(self._recs)]
        n = len(recs)
        agree = sum(r.candidate is r.decision.action for r in recs)
        resolved = [r for r in recs if is_resolved_chargeback(r.case)]
        counts = Counter(r.case.currency for r in resolved)
        currency = min(counts, key=lambda c: (-counts[c], c)) if counts else None
        priced = [
            price_action(
                _historical(r.case, tuple(r.movements.values())),
                r.decision.action,
                r.candidate,
                r.view_at,
                self._profile,
                self._cfg,
                MERCHANT_ACCOUNT,
            )
            for r in resolved
            if r.case.currency == currency
        ]
        same = [p for p in priced if p.basis == "observed"]
        diff = [p for p in priced if p.basis == "estimated"]

        def tot(xs: Sequence[float]) -> float | None:
            return round(math.fsum(xs), 2) if xs else None

        unit = f"minor units of {currency}" if currency else "no resolved cases yet"
        metrics = (
            Metric(name="agreement", basis="observed", value=agree / n if n else None, n=n),
            Metric(name="changed_decisions", basis="observed", value=float(n - agree), n=n),
            Metric(
                name="resolved_cases",
                basis="observed",
                value=float(len(resolved)),
                n=n,
                note="outcomes arrive through observe(); open cases carry no cost yet",
            ),
            Metric(
                name="production_cost",
                basis="observed",
                value=tot([p.baseline for p in priced]),
                n=len(priced),
                note=f"realised cost, {unit}",
            ),
            Metric(
                name="candidate_cost_same_action",
                basis="observed",
                value=tot([p.cost for p in same]),
                n=len(same),
                note=f"equal to production's realised cost on these cases, {unit}",
            ),
            Metric(
                name="candidate_cost_changed_action",
                basis="estimated",
                value=tot([p.cost for p in diff]),
                n=len(diff),
                low=tot([p.low for p in diff]),
                high=tot([p.high for p in diff]),
                note=f"priced as in the backtest from {self._profile_note}, {unit}",
            ),
            Metric(
                name="candidate_minus_production",
                basis="estimated" if diff else "observed",
                value=tot([p.cost - p.baseline for p in priced]),
                n=len(priced),
                low=tot([p.low - p.baseline for p in priced]),
                high=tot([p.high - p.baseline for p in priced]),
                note="negative is better; estimated once any priced decision changed",
            ),
        )
        prod: Counter[str] = Counter()
        cand: Counter[str] = Counter()
        for r in recs:
            prod.update(r.findings)
            cand.update(r.candidate_findings)
            prod.update(ledger_findings(r.case, tuple(r.movements.values())))
            prod.update(evidence_findings(r.case, r.decision.action))
        action_checks = ACTION_CHECKS | self._partner
        findings = tuple(
            FindingCount(
                check=c,
                kind=self._kind(c),
                production=prod[c],
                candidate=cand[c] if c in action_checks else None,
            )
            for c in sorted(set(prod) | set(cand))
        )
        by_reason: dict[str, dict[str, int]] = defaultdict(
            lambda: {"n": 0, "agree": 0, "changed": 0, "resolved": 0}
        )
        for r in recs:
            row = by_reason[r.case.source_reason]
            same_action = r.candidate is r.decision.action
            row["n"] += 1
            row["agree"] += same_action
            row["changed"] += not same_action
            row["resolved"] += is_resolved_chargeback(r.case)
        return ImportedShadowSummary(
            data="synthetic" if self._synthetic else "real (imported)",
            candidate=self.candidate.name,
            profile=self._profile_note,
            decisions=n,
            resolved=len(resolved),
            open=n - len(resolved),
            currency=currency,
            metrics=metrics,
            findings=findings,
            by_reason={k: by_reason[k] for k in sorted(by_reason)},
        )


__all__ = [
    "ImportedBatchResult",
    "ImportedDecision",
    "ImportedDecisionBatch",
    "ImportedObserveResult",
    "ImportedOutcome",
    "ImportedOutcomeBatch",
    "ImportedShadowRecorder",
    "ImportedShadowSummary",
    "Metric",
    "contest_after_deadline",
    "evidence_findings",
    "ledger_findings",
]
