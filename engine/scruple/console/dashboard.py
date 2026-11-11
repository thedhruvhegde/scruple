"""The purchaser's dashboard: view models for /console/dashboard and its four pages.

Pure reads over one tenant: its gate (registry, decisions, audit chain, policy), the
tables that share the gate's database (jobs, usage, billing pushes, imported shadow
summaries), and its own data folder (`scruple.console.tenant_files`: case store,
backtests, pilot run, eval runs, anchors). Nothing here writes. Every query names the
tenant; another tenant's rows are never read.

Every number carries its basis, and the pages print it next to the number:

    observed    read from records (ledger postings, case states, decisions, jobs)
    estimated   priced by a model (a changed backtest action, a held-out replay)
    synthetic   computed on simulated cases (the gate's held-out set, the canary,
                the demo tenant): says nothing about real outcomes

Observed and estimated money are never added together.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from scruple.audit.anchor import check_statement
from scruple.audit.models import AuditEntry
from scruple.backtest.engine import BacktestReport
from scruple.console import charts
from scruple.console.charts import Chart, Point, TimePoint
from scruple.console.sources import ConsoleSources
from scruple.console.tenant_files import EvalRun, TenantFiles, TenantMarker, tenant_files
from scruple.gate.canary import CanaryState
from scruple.gate.models import DecisionRecord, Outcome, Stage
from scruple.gate.shadow_imported import ImportedShadowSummary
from scruple.ingest.models import CaseState, ExternalCase, LedgerMovement
from scruple.jobs.models import Job, JobStatus
from scruple.registry import RuleStatus, RuleVersion
from scruple.service import AgentGate
from scruple.storage.sql import SQLStorage

Period = Literal["30d", "90d", "365d", "all"]
PERIODS: tuple[Period, ...] = ("30d", "90d", "365d", "all")
PERIOD_DAYS: dict[str, int | None] = {"30d": 30, "90d": 90, "365d": 365, "all": None}
PERIOD_LABEL: dict[str, str] = {
    "30d": "last 30 days",
    "90d": "last 90 days",
    "365d": "last 365 days",
    "all": "all time",
}
MAX_JOBS = 500
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:/[^\s/()]+){2,}")
RECENT = 25


@dataclass(frozen=True)
class Window:
    period: Period
    now: datetime
    start: datetime | None

    @property
    def label(self) -> str:
        return PERIOD_LABEL[self.period]

    def has(self, at: datetime | None) -> bool:
        if at is None:
            return self.start is None
        a = at if at.tzinfo else at.replace(tzinfo=UTC)
        return self.start is None or a >= self.start


def window(period: Period, now: datetime) -> Window:
    days = PERIOD_DAYS[period]
    return Window(period, now, None if days is None else now - timedelta(days=days))


@dataclass(frozen=True)
class Context:
    """Everything one request may read: the caller's gate and the caller's files."""

    gate: AgentGate
    tenant: str
    files: TenantFiles | None
    win: Window

    @property
    def marker(self) -> TenantMarker | None:
        return self.files.marker() if self.files is not None else None

    @property
    def synthetic(self) -> bool:
        m = self.marker
        return bool(m and m.synthetic)


def context(gate: AgentGate, tenant: str, src: ConsoleSources, period: Period) -> Context:
    return Context(gate, tenant, tenant_files(src.data, tenant), window(period, gate.clock.now()))


def _sql(gate: AgentGate) -> SQLStorage | None:
    s = gate.storage
    return s if isinstance(s, SQLStorage) else None


def _week(at: datetime) -> date:
    d = at.astimezone(UTC).date()
    return d - timedelta(days=d.weekday())


# --- 1. savings -------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestRow:
    name: str
    source: str  # "job job-000012" | "file" | "pilot run"
    at: datetime | None
    report: BacktestReport

    @property
    def observed_delta(self) -> float:
        """Cases where the candidate did what history did: 0 by construction, but the
        observed part of the candidate's cost is printed beside history's."""
        return self.report.candidate_cost_observed

    @property
    def delta(self) -> float:
        """Candidate minus history, in minor units (negative = saving). Estimated
        whenever any action changed."""
        return self.report.candidate_cost - self.report.baseline_cost

    @property
    def delta_low(self) -> float:
        return self.report.candidate_cost_low - self.report.baseline_cost

    @property
    def delta_high(self) -> float:
        return self.report.candidate_cost_high - self.report.baseline_cost

    @property
    def basis(self) -> str:
        return "estimated" if self.report.n_estimated else "observed"

    @property
    def data_label(self) -> str:
        """The report's dataset label without server file paths (a page shows where data
        came from, never where the operator keeps it), clipped to a line or two."""
        text = _PATH.sub(lambda m: m.group(0).rsplit("/", 1)[-1], self.report.dataset)
        return text if len(text) <= 140 else text[:139] + "..."


@dataclass(frozen=True)
class ShadowRow:
    """One shadow decision on the gate's synthetic held-out replay."""

    rec: DecisionRecord
    n: int
    mean_improvement: float  # per case, baseline minus candidate loss (positive = better)
    ci_low: float
    ci_high: float
    harm: float  # summed extra loss on the case types that got worse (>= 0)
    e_regression: float | None
    e_certify: float | None

    @property
    def total(self) -> float:
        return self.mean_improvement * self.n

    @property
    def ref(self) -> str:
        return f"{self.rec.decision.rule_id}@v{self.rec.decision.version}"

    @property
    def blocked(self) -> bool:
        return self.rec.decision.outcome is Outcome.BLOCK

    @property
    def avoided(self) -> float:
        """What a blocked change was estimated to cost had it gone live: its net loss on
        the replay if it was worse on average, else the loss on its worse case types."""
        return max(-self.total, self.harm, 0.0)


@dataclass(frozen=True)
class ImportedRow:
    rule_id: str
    version: int
    at: datetime
    summary: ImportedShadowSummary
    history: tuple[tuple[datetime, float], ...]  # candidate_minus_production over time

    @property
    def ref(self) -> str:
        return f"{self.rule_id}@v{self.version}"

    def metric(self, name: str) -> Any:
        try:
            return self.summary.metric(name)
        except StopIteration:
            return None


@dataclass(frozen=True)
class CanaryRow:
    rec: DecisionRecord
    treatment: int
    delta_per_case: float  # treatment minus control mean cost (positive = worse)
    sequential: str | None  # the sequential test's call: harm, benefit, no_decision

    @property
    def ref(self) -> str:
        return f"{self.rec.decision.rule_id}@v{self.rec.decision.version}"


@dataclass(frozen=True)
class Savings:
    backtests: tuple[BacktestRow, ...]
    shadow: tuple[ShadowRow, ...]
    blocked: tuple[ShadowRow, ...]
    imported: tuple[ImportedRow, ...]
    canary: tuple[CanaryRow, ...]
    backtest_observed: float  # history's realised cost over the backtests (observed)
    backtest_estimated: float  # candidate minus history, summed (estimated)
    avoided: float  # blocked changes, estimated on synthetic replay
    shadow_net: float  # promoted changes, estimated on synthetic replay
    imported_net: float | None  # candidate minus production on imported cases
    imported_basis: str
    charts: dict[str, Chart] = field(default_factory=dict)


def _shadow_row(rec: DecisionRecord) -> ShadowRow | None:
    s = rec.decision.shadow
    if s is None:
        return None
    harm = sum(max(sl.delta, 0.0) * sl.n for sl in s.slices)
    ev = s.evalue
    if ev is not None:
        harm = max(harm, sum(max(t.mean_diff, 0.0) * t.n for t in ev.types if t.regressed))
    return ShadowRow(
        rec=rec,
        n=s.n_cases,
        mean_improvement=s.mean_improvement_usd_minor,
        ci_low=s.ci_low,
        ci_high=s.ci_high,
        harm=harm,
        e_regression=None if ev is None else ev.pooled_regression_e_value,
        e_certify=None if ev is None else ev.pooled_certify_e_value,
    )


def tenant_jobs(gate: AgentGate) -> list[Job]:
    from scruple.jobs.queue import JobQueue
    from scruple.storage.lazy import connection_of

    try:
        conn, dialect = connection_of(gate.storage)
    except TypeError:
        return []
    q = JobQueue(conn, dialect, clock=gate.clock)
    return q.list_jobs(tenant_id=gate.storage.tenant, limit=MAX_JOBS)


def backtest_rows(ctx: Context, jobs: Sequence[Job]) -> list[BacktestRow]:
    out: list[BacktestRow] = []
    for j in jobs:
        if j.kind != "backtest" or j.status is not JobStatus.SUCCEEDED or not j.result:
            continue
        body = j.result.get("report")
        if not isinstance(body, dict):
            continue
        try:
            rep = BacktestReport.model_validate(body)
        except ValueError:
            continue
        out.append(BacktestRow(f"{j.rule_id}@v{j.version}", f"job {j.job_id}", j.finished_at, rep))
    if ctx.files is not None:
        for name, entry in ctx.files.reports().items():
            try:
                rep = entry.load()
            except (OSError, ValueError):
                continue
            src = "pilot run" if name == "pilot" else "report file"
            out.append(BacktestRow(rep.candidate, src, rep.as_of, rep))
    return [r for r in out if ctx.win.has(r.at)]


def imported_rows(ctx: Context) -> list[ImportedRow]:
    s = _sql(ctx.gate)
    if s is None:
        return []
    rows = s.connection.query(
        "SELECT DISTINCT rule_id, version FROM imported_shadow_summaries WHERE tenant_id=? "
        "ORDER BY rule_id, version",
        (ctx.tenant,),
    )
    out: list[ImportedRow] = []
    for r in rows:
        snaps = s.imported_shadow.snapshots(ctx.tenant, r["rule_id"], int(r["version"]))
        series: list[tuple[datetime, float]] = []
        last: ImportedShadowSummary | None = None
        last_at: datetime | None = None
        for snap in snaps:
            try:
                summ = ImportedShadowSummary.model_validate_json(snap.body_json)
            except ValueError:
                continue
            last, last_at = summ, snap.at
            m = next((x for x in summ.metrics if x.name == "candidate_minus_production"), None)
            if m is not None and m.value is not None and ctx.win.has(snap.at):
                series.append((snap.at, m.value))
        if last is not None and last_at is not None:
            out.append(ImportedRow(r["rule_id"], int(r["version"]), last_at, last, tuple(series)))
    return out


def savings(ctx: Context, jobs: Sequence[Job] | None = None) -> Savings:
    jobs = tenant_jobs(ctx.gate) if jobs is None else jobs
    decisions = [r for r in ctx.gate.decisions() if ctx.win.has(r.decision.created_at)]
    shadow = [
        row
        for r in decisions
        if r.decision.stage is Stage.SHADOW and (row := _shadow_row(r)) is not None
    ]
    blocked = [r for r in shadow if r.blocked]
    promoted = [r for r in shadow if r.rec.decision.outcome is Outcome.PROMOTE]
    canary = []
    for r in decisions:
        c = r.decision.canary
        if r.decision.stage is not Stage.CANARY or c is None:
            continue
        canary.append(
            CanaryRow(
                r,
                c.treatment_cases,
                c.treatment_mean_cost_usd_minor - c.control_mean_cost_usd_minor,
                None if c.sequential is None else c.sequential.decision,
            )
        )
    bts = backtest_rows(ctx, jobs)
    imp = imported_rows(ctx)
    imp_vals = [
        m.value
        for i in imp
        if (m := i.metric("candidate_minus_production")) is not None and m.value is not None
    ]
    imp_basis = (
        "estimated"
        if any(
            (m := i.metric("candidate_minus_production")) and m.basis == "estimated" for i in imp
        )
        else "observed"
    )
    sv = Savings(
        backtests=tuple(bts),
        shadow=tuple(shadow),
        blocked=tuple(blocked),
        imported=tuple(imp),
        canary=tuple(canary),
        backtest_observed=math.fsum(b.report.baseline_cost for b in bts),
        backtest_estimated=math.fsum(b.delta for b in bts),
        avoided=math.fsum(b.avoided for b in blocked),
        shadow_net=math.fsum(p.total for p in promoted),
        imported_net=math.fsum(imp_vals) if imp_vals else None,
        imported_basis=imp_basis,
    )
    sv.charts.update(savings_charts(sv))
    return sv


def savings_charts(sv: Savings) -> dict[str, Chart]:
    out: dict[str, Chart] = {}
    run = 0.0
    pts = []
    for b in sorted(sv.blocked, key=lambda x: x.rec.decision.created_at):
        run += b.avoided
        pts.append(TimePoint(b.rec.decision.created_at, run))
    out["avoided"] = charts.steps(
        "estimated cost avoided by blocked changes, running total",
        pts,
        "usd",
        basis="estimated, synthetic held-out replay",
    )
    out["shadow"] = charts.bars(
        "net change on the held-out replay per decision (positive = better)",
        [
            Point(
                s.ref,
                s.total,
                s.ci_low * s.n,
                s.ci_high * s.n,
                cls="neg" if s.blocked else ("pos" if s.total >= 0 else "neg"),
            )
            for s in sv.shadow
        ],
        "usd",
        basis="estimated, synthetic held-out replay, interval from the decision",
    )
    out["backtest"] = charts.bars(
        "candidate minus history per backtest (negative = saving)",
        [
            Point(b.name, b.delta, b.delta_low, b.delta_high, cls="pos" if b.delta <= 0 else "neg")
            for b in sv.backtests
        ],
        "usd",
        basis="estimated where an action changed; history's cost is observed",
    )
    for i in sv.imported:
        out[f"imported:{i.ref}"] = charts.steps(
            f"{i.ref}: candidate minus production on imported cases (negative = better)",
            [TimePoint(a, v) for a, v in i.history],
            "usd",
            basis=f"{i.summary.data}; estimated once a decision changed",
            cls="hi",
        )
    return out


# --- 2. payments --------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrencyTotals:
    currency: str
    disputes: int
    amount: int
    fees: int
    refunded: int
    withdrawn: int  # dispute debits (negative), observed
    reinstated: int  # dispute credits


@dataclass(frozen=True)
class Payments:
    store: bool
    kind: str | None  # "synthetic" | "real" | None
    cases: int
    by_state: tuple[tuple[str, int], ...]
    by_reason: tuple[tuple[str, int], ...]
    by_source: tuple[tuple[str, int], ...]
    totals: tuple[CurrencyTotals, ...]
    deadlines: dict[str, int]
    due_soon: tuple[ExternalCase, ...]
    win_rate: float | None
    livemode: int
    usage: tuple[tuple[str, str, int, int], ...]  # (period, meter, quantity, events)
    usage_totals: dict[str, int]
    pushes: tuple[dict[str, object], ...]
    pushed_totals: dict[str, int]
    unpushed: dict[str, int]
    charts: dict[str, Chart] = field(default_factory=dict)


def _load_cases(path: str) -> tuple[list[ExternalCase], list[LedgerMovement], str | None]:
    from scruple.ingest.store import CaseStore, StoreError

    try:
        with CaseStore(path, create=False) as store:
            return [sc.case for sc in store.cases()], store.movements(), store.kind()
    except (StoreError, OSError, ValueError):
        return [], [], None


DEADLINE_LABELS = {
    "met": "evidence submitted, not past due",
    "missed": "evidence past due",
    "no_response": "resolved with no evidence submitted",
    "open_due": "open, evidence still due",
    "open_overdue": "open, due date passed",
}


def payments(ctx: Context) -> Payments:
    store = ctx.files.case_store() if ctx.files is not None else None
    cases: list[ExternalCase] = []
    moves: list[LedgerMovement] = []
    kind = None
    if store is not None:
        cases, moves, kind = _load_cases(str(store))
    cases = [c for c in cases if ctx.win.has(c.opened_at)]
    ids = {c.external_id for c in cases}
    moves = [m for m in moves if m.case_external_id in ids]
    now = ctx.win.now
    by_state = Counter(c.state.value for c in cases)
    decided = by_state["won"] + by_state["lost"]
    deadlines: Counter[str] = Counter({k: 0 for k in DEADLINE_LABELS})
    for c in cases:
        if c.state is CaseState.OPEN:
            due = c.evidence_due_at
            deadlines["open_overdue" if due is not None and due < now else "open_due"] += 1
        elif c.evidence_past_due:
            deadlines["missed"] += 1
        elif c.evidence_submitted:
            deadlines["met"] += 1
        else:
            deadlines["no_response"] += 1
    soon = sorted(
        (
            c
            for c in cases
            if c.state is CaseState.OPEN
            and c.evidence_due_at is not None
            and now <= c.evidence_due_at <= now + timedelta(days=7)
        ),
        key=lambda c: c.evidence_due_at or now,
    )
    totals = []
    for ccy in sorted({c.currency for c in cases}):
        cs = [c for c in cases if c.currency == ccy]
        ms = [m for m in moves if m.currency == ccy]
        totals.append(
            CurrencyTotals(
                currency=ccy,
                disputes=len(cs),
                amount=sum(c.amount_minor for c in cs),
                fees=sum(m.fee_minor for m in ms),
                refunded=sum((c.facts.amount_refunded_minor or 0) for c in cs if c.facts),
                withdrawn=sum(m.amount_minor for m in ms if m.amount_minor < 0),
                reinstated=sum(m.amount_minor for m in ms if m.amount_minor > 0),
            )
        )
    usage, usage_totals = _usage(ctx)
    pushes, pushed_totals = _pushes(ctx)
    unpushed = {m: usage_totals.get(m, 0) - pushed_totals.get(m, 0) for m in usage_totals}
    p = Payments(
        store=store is not None,
        kind=kind,
        cases=len(cases),
        by_state=tuple(sorted(by_state.items(), key=lambda kv: -kv[1])),
        by_reason=tuple(Counter(c.source_reason for c in cases).most_common()),
        by_source=tuple(Counter(c.source for c in cases).most_common()),
        totals=tuple(totals),
        deadlines=dict(deadlines),
        due_soon=tuple(soon[:RECENT]),
        win_rate=by_state["won"] / decided if decided else None,
        livemode=sum(1 for c in cases if c.livemode),
        usage=tuple(usage),
        usage_totals=usage_totals,
        pushes=tuple(pushes),
        pushed_totals=pushed_totals,
        unpushed=unpushed,
    )
    p.charts.update(payments_charts(p, cases))
    return p


def _usage(ctx: Context) -> tuple[list[tuple[str, str, int, int]], dict[str, int]]:
    from scruple.usage.meter import usage_store_for

    us = usage_store_for(ctx.gate.storage, ctx.gate.clock)
    if us is None:
        return [], {}
    start = ctx.win.start.date() if ctx.win.start is not None else None
    period: Literal["day", "month"] = "day" if ctx.win.period == "30d" else "month"
    aggs = us.aggregate(ctx.tenant, period, start=start)
    rows = [(a.period_start.isoformat(), a.meter.value, a.quantity, a.events) for a in aggs]
    totals: dict[str, int] = defaultdict(int)
    for _, m, q, _ in rows:
        totals[m] += q
    return rows, dict(totals)


def _pushes(ctx: Context) -> tuple[list[dict[str, object]], dict[str, int]]:
    from scruple.billing.push import ledger_for

    led = ledger_for(ctx.gate.storage)
    if led is None:
        return [], {}
    rows = []
    for r in led.rows(ctx.tenant):
        try:
            day = date.fromisoformat(str(r["period_start"]))
        except ValueError:
            continue
        if ctx.win.has(datetime(day.year, day.month, day.day, tzinfo=UTC)):
            rows.append(r)
    totals: dict[str, int] = defaultdict(int)
    for r in rows:
        totals[str(r["meter"])] += int(str(r["delta"]))
    return rows, dict(totals)


def payments_charts(p: Payments, cases: Sequence[ExternalCase]) -> dict[str, Chart]:
    weeks: Counter[date] = Counter()
    amounts: Counter[date] = Counter()
    main = p.totals[0].currency if p.totals else None
    if p.totals:
        main = max(p.totals, key=lambda t: t.disputes).currency
    for c in cases:
        w = _week(c.opened_at)
        weeks[w] += 1
        if c.currency == main:
            amounts[w] += c.amount_minor
    span = _weeks(sorted(weeks))
    out = {
        "weekly": charts.bars(
            "disputes opened per week",
            [Point(w.strftime("%m-%d"), weeks[w]) for w in span],
            "count",
            basis="observed (as exported)",
        ),
        "amounts": charts.bars(
            f"disputed amount per week ({main or 'no currency'})",
            [Point(w.strftime("%m-%d"), amounts[w]) for w in span],
            "usd",
            basis="observed (as exported)",
        ),
        "reasons": charts.hbars(
            "reason code mix", [Point(r, n) for r, n in p.by_reason], basis="observed"
        ),
        "states": charts.hbars(
            "outcome",
            [Point(s, n, cls=_state_cls(s)) for s, n in p.by_state],
            basis="observed",
        ),
        "deadlines": charts.hbars(
            "evidence deadlines",
            [
                Point(k.replace("_", " "), v, cls="neg" if k in ("missed", "open_overdue") else "")
                for k, v in p.deadlines.items()
            ],
            basis="observed",
        ),
    }
    meters = sorted({m for _, m, _, _ in p.usage})
    for m in meters:
        out[f"usage:{m}"] = charts.bars(
            m.replace("_", " "),
            [Point(per[5:] if len(per) > 7 else per, q) for per, mm, q, _ in p.usage if mm == m],
            "count",
            height=140,
            basis="observed (metered from gate decisions)",
        )
    return out


def _weeks(seen: Sequence[date]) -> list[date]:
    if not seen:
        return []
    out: list[date] = []
    w = seen[0]
    while w <= seen[-1] and len(out) < 120:
        out.append(w)
        w += timedelta(days=7)
    return out


def _state_cls(s: str) -> str:
    return {"won": "pos", "lost": "neg", "accepted": "dim", "open": "hi"}.get(s, "dim")


# --- 3. routing ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteRow:
    case_type: str
    n: int
    by_rule: tuple[tuple[str, int], ...]  # decided_by -> cases


@dataclass(frozen=True)
class RampRow:
    rv: RuleVersion
    state: CanaryState | None
    chart: Chart | None

    @property
    def percent(self) -> float:
        return self.state.percent if self.state is not None else 0.0


@dataclass(frozen=True)
class AgentRow:
    agent: str
    rules: tuple[str, ...]  # active or in-flight rule versions it proposed
    runs: tuple[EvalRun, ...]


@dataclass(frozen=True)
class Change:
    entry: AuditEntry
    ref: str
    what: str
    approvers: tuple[str, ...]


@dataclass(frozen=True)
class Routing:
    policy: Any  # PolicyRecord | None
    approval: Any  # ApprovalPolicy | None
    active: tuple[RuleVersion, ...]
    routes: tuple[RouteRow, ...]
    replay_seed: int
    replay_cases: int
    ramps: tuple[RampRow, ...]
    agents: tuple[AgentRow, ...]
    changes: tuple[Change, ...]
    charts: dict[str, Chart] = field(default_factory=dict)


ROUTING_EVENTS = ("rule.", "canary.", "approval.", "policy.set", "decision.recorded")


def route_table(gate: AgentGate, seed: int, n: int) -> tuple[RouteRow, ...]:
    """Which rule decides each case type, on the public replay set under the live
    policy (synthetic cases; the case type is the simulator's hidden label)."""
    from scruple.gate.engine import policy_from
    from scruple.sim.engine import Simulator
    from scruple.sim.generator import generate_cases

    cases = generate_cases(seed, n, gate.config.profile, prefix="rp")
    run = Simulator(cases).run(policy_from(gate.registry.active_rules()))
    by: dict[str, Counter[str]] = defaultdict(Counter)
    for r in run.results:
        by[r.scenario.value][r.decided_by] += 1
    return tuple(
        RouteRow(t, sum(c.values()), tuple(c.most_common())) for t, c in sorted(by.items())
    )


def _ref(p: Mapping[str, Any]) -> str:
    rid, v = p.get("rule_id"), p.get("version")
    return f"{rid}@v{v}" if isinstance(rid, str) and v is not None else ""


def _what(e: AuditEntry) -> str:
    p = e.payload
    t = e.event_type
    if t in ("rule.status_changed", "rule.activated", "rule.rolled_back"):
        what = f"{p.get('from_status', '?')} -> {p.get('to_status', '?')}"
        reason = p.get("reason")
        return f"{what}: {reason}" if reason else what
    if t == "decision.recorded":
        return f"{p.get('stage')} {p.get('outcome')}"
    if t.startswith("canary."):
        st = p.get("stage_index")
        pct = p.get("percent")
        bits = [t.removeprefix("canary.").replace("_", " ")]
        if pct is not None:
            bits.append(f"{pct}%")
        elif st is not None:
            bits.append(f"stage {int(st) + 1}")
        return " ".join(bits)
    if t == "policy.set":
        return f"policy {p.get('name')} #{p.get('number')}"
    return t.split(".", 1)[-1].replace("_", " ")


def routing_changes(entries: Sequence[AuditEntry], win: Window, limit: int = 40) -> list[Change]:
    granted: dict[str, list[str]] = defaultdict(list)
    out: list[Change] = []
    for e in entries:
        if not e.event_type.startswith(ROUTING_EVENTS):
            continue
        ref = _ref(e.payload)
        if e.event_type == "approval.granted" and ref:
            granted[ref].append(e.actor)
        if e.event_type == "decision.recorded" and e.payload.get("stage") not in (
            "activation",
            "rollback",
        ):
            continue
        approvers: tuple[str, ...] = ()
        if e.event_type == "rule.activated" and ref:
            approvers = tuple(dict.fromkeys(granted.get(ref, []) or [e.actor]))
        if win.has(e.at):
            out.append(Change(e, ref, _what(e), approvers))
    return out[::-1][:limit]


def routing(ctx: Context, src: ConsoleSources, routes: tuple[RouteRow, ...]) -> Routing:
    from scruple.policy.apply import current_policy

    gate = ctx.gate
    try:
        pol = current_policy(gate)
    except TypeError:
        pol = None
    versions = gate.registry.all_versions()
    active = tuple(sorted(gate.registry.active_rules(), key=lambda r: (r.spec.priority, r.ref)))
    ramps = []
    for rv in versions:
        if rv.status is not RuleStatus.CANARY:
            continue
        st = gate.canary_state(rv.rule_id, rv.version)
        chart = (
            charts.ramp(st.schedule.stages, st.stage_index, st.status) if st is not None else None
        )
        ramps.append(RampRow(rv, st, chart))
    runs = ctx.files.eval_runs() if ctx.files is not None else []
    agents: dict[str, list[str]] = defaultdict(list)
    for rv in versions:
        if rv.status in (RuleStatus.ACTIVE, RuleStatus.CANARY, RuleStatus.SHADOW):
            agents[rv.provenance.agent_id].append(rv.ref)
    for er in runs:
        agents.setdefault(er.agent, [])
    agent_rows = tuple(
        AgentRow(a, tuple(refs), tuple(er for er in runs if er.agent == a))
        for a, refs in sorted(agents.items())
    )
    rt = Routing(
        policy=pol,
        approval=gate.approval_policy(),
        active=active,
        routes=routes,
        replay_seed=src.replay_seed,
        replay_cases=src.replay_cases,
        ramps=tuple(ramps),
        agents=agent_rows,
        changes=tuple(routing_changes(gate.audit.entries(), ctx.win)),
    )
    for r in routes:
        rt.charts[f"route:{r.case_type}"] = charts.hbars(
            r.case_type.replace("_", " "),
            [Point(rule, n, cls="dim" if rule == "default" else "") for rule, n in r.by_rule],
            basis="synthetic replay",
        )
    return rt


# --- 4. runs and compute -------------------------------------------------------------------


@dataclass(frozen=True)
class JobRow:
    job: Job
    claimed_at: datetime | None
    duration_s: float | None  # last claim to finish, from the job's own events
    worker: str | None
    cases: int | None


@dataclass(frozen=True)
class WorkerRow:
    worker: str
    last_seen: datetime
    finished: int
    failed: int
    running: str | None  # job id held now
    lease_expires_at: datetime | None

    @property
    def status(self) -> str:
        if self.running is None:
            return "idle"
        return "running"


@dataclass(frozen=True)
class EvalRow:
    run: EvalRun
    episodes: int
    input_tokens: int | None
    output_tokens: int | None
    requests: int | None
    cost_usd: float | None
    mean_reward: float | None


@dataclass(frozen=True)
class GateRow:
    rec: DecisionRecord
    cases: int
    duration_s: float | None
    job: str | None


@dataclass(frozen=True)
class Compute:
    by_status: dict[str, int]
    jobs: tuple[JobRow, ...]
    workers: tuple[WorkerRow, ...]
    gate_runs: tuple[GateRow, ...]
    cases_replayed: int
    evals: tuple[EvalRow, ...]
    tokens: tuple[int, int]
    cost_usd: float | None
    integrity: Any  # IntegrityReport
    anchor: Any  # Anchor | None
    anchor_check: Any  # ChainCheck | None
    charts: dict[str, Chart] = field(default_factory=dict)


def _events(gate: AgentGate) -> list[dict[str, Any]]:
    s = _sql(gate)
    if s is None:
        return []
    return [
        dict(r)
        for r in s.connection.query(
            "SELECT job_id, at, event, worker FROM job_events WHERE tenant_id=? ORDER BY id",
            (gate.storage.tenant,),
        )
    ]


def _cases_of_result(result: Mapping[str, Any] | None) -> int | None:
    if not result:
        return None
    rep = result.get("report")
    if isinstance(rep, dict) and isinstance(rep.get("n_included"), int):
        return int(rep["n_included"])
    return None


def compute(ctx: Context, jobs: Sequence[Job] | None = None) -> Compute:
    gate = ctx.gate
    jobs = tenant_jobs(gate) if jobs is None else jobs
    events = _events(gate)
    claimed: dict[int, datetime] = {}
    by_worker: dict[str, dict[str, Any]] = {}
    for e in events:
        at = datetime.fromisoformat(str(e["at"]))
        n = int(e["job_id"])
        if e["event"] == "claimed":
            claimed[n] = at
        w = e["worker"]
        if w:
            row = by_worker.setdefault(str(w), {"last": at, "ok": 0, "failed": 0})
            row["last"] = max(row["last"], at)
            row["ok"] += e["event"] == "succeeded"
            row["failed"] += e["event"] == "failed"
    rows = []
    for j in jobs:
        c = claimed.get(j.number)
        dur = (j.finished_at - c).total_seconds() if c is not None and j.finished_at else None
        rows.append(JobRow(j, c, dur, j.lease_owner, _cases_of_result(j.result)))
    shown = [r for r in rows if ctx.win.has(r.job.created_at)]
    running = {j.lease_owner: j for j in jobs if j.status is JobStatus.RUNNING and j.lease_owner}
    workers = tuple(
        WorkerRow(
            worker=w,
            last_seen=d["last"],
            finished=int(d["ok"]),
            failed=int(d["failed"]),
            running=running[w].job_id if w in running else None,
            lease_expires_at=running[w].lease_expires_at if w in running else None,
        )
        for w, d in sorted(by_worker.items(), key=lambda kv: kv[1]["last"], reverse=True)
    )
    by_decision: dict[str, JobRow] = {}
    for r in rows:
        if r.job.kind == "gate_evaluation" and r.job.result:
            for d in r.job.result.get("decisions", []) or []:
                if isinstance(d, dict) and isinstance(d.get("decision_id"), str):
                    by_decision[d["decision_id"]] = r
    gate_runs = []
    for rec in gate.decisions():
        d = rec.decision
        if d.stage not in (Stage.SHADOW, Stage.CANARY, Stage.ASSURANCE):
            continue
        if not ctx.win.has(d.created_at):
            continue
        n = d.shadow.n_cases if d.shadow else (d.canary.live_cases if d.canary else 0)
        jr = by_decision.get(d.decision_id)
        gate_runs.append(
            GateRow(rec, n, jr.duration_s if jr else None, jr.job.job_id if jr else None)
        )
    runs = ctx.files.eval_runs() if ctx.files is not None else []
    evals = []
    for er in runs:
        recs = er.records

        def tot(name: str, recs: Sequence[Any] = recs) -> int | None:
            vals = [getattr(x, name) for x in recs if getattr(x, name) is not None]
            return sum(vals) if vals else None

        costs = [x.cost_usd for x in recs if x.cost_usd is not None]
        rewards = [v for x in recs if (v := _num(x.score.get("reward"))) is not None]
        evals.append(
            EvalRow(
                run=er,
                episodes=len(recs),
                input_tokens=tot("input_tokens"),
                output_tokens=tot("output_tokens"),
                requests=tot("requests"),
                cost_usd=round(math.fsum(costs), 6) if costs else None,
                mean_reward=(math.fsum(rewards) / len(rewards)) if rewards else None,
            )
        )
    integrity = gate.verify_integrity()
    anchors = ctx.files.anchors() if ctx.files is not None else []
    anchor = max(anchors, key=lambda a: a.statement.entries) if anchors else None
    check = None
    if anchor is not None:
        check = check_statement(gate.audit.entries(), anchor.statement, ctx.tenant)
    costs_all = [e.cost_usd for e in evals if e.cost_usd is not None]
    cp = Compute(
        by_status={s.value: sum(1 for r in shown if r.job.status is s) for s in JobStatus},
        jobs=tuple(sorted(shown, key=lambda r: r.job.number, reverse=True)[: RECENT * 2]),
        workers=workers,
        gate_runs=tuple(gate_runs[::-1]),
        cases_replayed=sum(g.cases for g in gate_runs),
        evals=tuple(evals),
        tokens=(
            sum(e.input_tokens or 0 for e in evals),
            sum(e.output_tokens or 0 for e in evals),
        ),
        cost_usd=round(math.fsum(costs_all), 6) if costs_all else None,
        integrity=integrity,
        anchor=anchor,
        anchor_check=check,
    )
    cp.charts.update(compute_charts(cp))
    return cp


def _num(x: object) -> float | None:
    if isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(float(x)):
        return float(x)
    return None


def compute_charts(cp: Compute) -> dict[str, Chart]:
    done = [r for r in cp.jobs if r.duration_s is not None][::-1][-24:]
    return {
        "status": charts.hbars(
            "jobs by status",
            [
                Point(s, n, cls={"failed": "neg", "running": "hi", "queued": "dim"}.get(s, ""))
                for s, n in cp.by_status.items()
            ],
            basis="observed",
        ),
        "durations": charts.bars(
            "job duration, seconds (claim to finish)",
            [
                Point(
                    r.job.job_id.removeprefix("job-").lstrip("0") or "0",
                    r.duration_s or 0.0,
                    cls="neg" if r.job.status is JobStatus.FAILED else "pos",
                )
                for r in done
            ],
            "count",
            basis="observed (job events)",
        ),
        "cases": charts.bars(
            "cases replayed per gate run",
            [Point(g.rec.decision.decision_id[-4:], g.cases) for g in cp.gate_runs[::-1][-24:]],
            "count",
            basis="synthetic held-out and canary cases",
        ),
        "tokens": charts.bars(
            "tokens per eval run (input + output)",
            [
                Point(f"{e.run.agent[:10]}", (e.input_tokens or 0) + (e.output_tokens or 0))
                for e in cp.evals
            ],
            "count",
            basis="recorded by the run",
        ),
    }


# --- the overview ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Overview:
    savings: Savings
    payments: Payments
    routing: Routing
    compute: Compute


def counts(items: Iterable[str]) -> list[tuple[str, int]]:
    return Counter(items).most_common()


def pretty_json(x: object) -> str:
    return json.dumps(x, sort_keys=True, default=str)


__all__ = [
    "PERIODS",
    "PERIOD_LABEL",
    "Compute",
    "Context",
    "Overview",
    "Payments",
    "Period",
    "Routing",
    "Savings",
    "compute",
    "context",
    "payments",
    "route_table",
    "routing",
    "savings",
    "tenant_jobs",
    "window",
]
