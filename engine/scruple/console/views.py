"""View models for the console pages. Pure reads: nothing here writes to storage.

Each builder turns gate state, an `EpisodeRecord` or a replay into plain data the
templates print. Templates escape everything (Jinja autoescape), so these functions
never build HTML.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean
from typing import Any

from scruple.audit.models import AuditEntry
from scruple.ci.scorecard import verdict_of
from scruple.console.fmt import minor
from scruple.console.sources import AgentRun
from scruple.gate.approval import (
    APPROVAL_EVENTS,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalStatus,
    evaluate_approvals,
    project_approvals,
    project_policy,
)
from scruple.gate.canary import CanaryState
from scruple.gate.engine import as_policy_rule, policy_from
from scruple.gate.models import DecisionRecord, Outcome, Stage
from scruple.registry import RuleStatus, RuleVersion
from scruple.service import EVENT_REJECTED, AgentGate
from scruple.sim.engine import RunResult, Simulator
from scruple.sim.generator import generate_cases
from scruple.sim.ledger import JournalEntry
from scruple.sim.models import CaseResult, DisputeCase
from scruple.sim.policy import Policy
from scruple.verifiers.checks import CheckResult, EvidenceRef, Scorecard
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.trace import EventKind, TraceEvent, verify_events

# --- runs and scores (181) ---------------------------------------------------------------


def verdict_for(records: Sequence[DecisionRecord]) -> str | None:
    """The gate verdict of the last gate decision (a manual rollback is not one)."""
    gate = [r for r in records if r.decision.stage is not Stage.ROLLBACK]
    return verdict_of(gate).value if gate else None


@dataclass(frozen=True)
class VersionRow:
    rv: RuleVersion
    decisions: int
    last: DecisionRecord | None
    verdict: str | None


def version_rows(gate: AgentGate) -> list[VersionRow]:
    by_version: dict[tuple[str, int], list[DecisionRecord]] = defaultdict(list)
    for rec in gate.decisions():
        by_version[(rec.decision.rule_id, rec.decision.version)].append(rec)
    rows = []
    for rv in gate.registry.all_versions():
        recs = by_version.get((rv.rule_id, rv.version), [])
        rows.append(VersionRow(rv, len(recs), recs[-1] if recs else None, verdict_for(recs)))
    rows.sort(key=lambda r: (r.rv.rule_id, -r.rv.version))
    return rows


@dataclass(frozen=True)
class AgentSummary:
    name: str
    description: str
    episodes: int
    gate_passed: int
    mean_reward: float
    mean_cost_usd_minor: float
    correct: int
    hard_failures: tuple[tuple[str, int], ...]


def agent_summary(run: AgentRun) -> AgentSummary:
    cards = run.scorecards
    recs = run.records
    fails: Counter[str] = Counter()
    for c in cards.values():
        fails.update(c.hard_failures)
    return AgentSummary(
        name=run.name,
        description=run.description,
        episodes=len(recs),
        gate_passed=sum(1 for c in cards.values() if c.gate),
        mean_reward=fmean([c.reward for c in cards.values()]) if cards else 0.0,
        mean_cost_usd_minor=fmean([r.cost_usd_minor for r in recs]) if recs else 0.0,
        correct=sum(1 for r in recs if r.outcome is not None and r.outcome.correct),
        hard_failures=tuple(sorted(fails.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


@dataclass(frozen=True)
class EpisodeRow:
    episode_id: str
    seed: int
    case_id: str
    family: str
    reason_code: str
    network: str
    action: str
    resolution: str
    cost_usd_minor: int
    correct: bool
    gate: bool
    reward: float
    failed: tuple[str, ...]


def last_action(r: EpisodeRecord) -> str:
    acts = r.actions(("accept", "represent", "escalate", "refund"))
    return acts[-1].tool or "none" if acts else ("lapsed" if r.lapsed else "none")


def episode_rows(run: AgentRun) -> list[EpisodeRow]:
    rows = []
    for r in run.records:
        card = run.scorecards[r.episode_id]
        rows.append(
            EpisodeRow(
                episode_id=r.episode_id,
                seed=r.seed,
                case_id=r.case.case_id,
                family=r.case.truth.family.value,
                reason_code=r.case.reason_code,
                network=r.case.network.value,
                action=last_action(r),
                resolution=r.resolution or ("lapsed" if r.lapsed else "open"),
                cost_usd_minor=r.cost_usd_minor,
                correct=r.outcome is not None and r.outcome.correct,
                gate=card.gate,
                reward=card.reward,
                failed=card.failed,
            )
        )
    return rows


# --- one rule version: decisions with their statistics ----------------------------------


@dataclass(frozen=True)
class VersionDetail:
    rv: RuleVersion
    decisions: tuple[DecisionRecord, ...]
    verdict: str | None
    canary: CanaryState | None
    history: tuple[AuditEntry, ...]
    pack_available: bool


def version_detail(gate: AgentGate, rule_id: str, version: int) -> VersionDetail:
    rv = gate.registry.get(rule_id, version)
    recs = tuple(r for r in gate.decisions(rule_id) if r.decision.version == version)
    history = tuple(
        e
        for e in gate.audit.entries()
        if e.payload.get("rule_id") == rule_id and _int(e.payload.get("version")) == version
    )
    return VersionDetail(
        rv=rv,
        decisions=recs,
        verdict=verdict_for(recs),
        canary=gate.canary_state(rule_id, version),
        history=history,
        pack_available=bool(recs),
    )


def _int(x: object) -> int | None:
    if isinstance(x, bool) or not isinstance(x, int | str):
        return None
    try:
        return int(x)
    except ValueError:
        return None


# --- case viewer (182) ---------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineRow:
    seq: int
    day: str
    actor: str
    what: str
    detail: str
    deadline: str | None


@dataclass(frozen=True)
class DocumentRow:
    doc_id: str
    kind: str
    issued_on: str
    content_hash: str
    field_names: tuple[str, ...]
    source: str  # env | agent | refund (from the trace)
    changed: bool  # differs from the generator's original
    forged: str | None  # hidden truth: how it was forged, if it was


@dataclass(frozen=True)
class CallRow:
    seq: int
    day: str
    tool: str
    args: str
    ok: bool
    error: str | None
    result: str


@dataclass(frozen=True)
class CheckRow:
    check: CheckResult
    links: tuple[tuple[str, str, str], ...]  # (label, href or "", note)


@dataclass(frozen=True)
class CaseView:
    record: EpisodeRecord
    scorecard: Scorecard
    timeline: tuple[TimelineRow, ...]
    journal: tuple[JournalEntry, ...]
    journal_balanced: tuple[bool, ...]
    documents: tuple[DocumentRow, ...]
    calls: tuple[CallRow, ...]
    checks: tuple[CheckRow, ...]
    trace: tuple[TraceEvent, ...]
    trace_broken: tuple[int, ...]
    trace_head_ok: bool
    case_fields: tuple[tuple[str, str], ...]


def _compact(x: Any, limit: int = 400) -> str:
    text = json.dumps(x, sort_keys=True, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _timeline(trace: Sequence[TraceEvent]) -> list[TimelineRow]:
    out: list[TimelineRow] = []
    for e in trace:
        d = e.data
        if e.kind is EventKind.TRANSITION:
            out.append(
                TimelineRow(
                    e.seq,
                    e.day.isoformat(),
                    e.actor.value,
                    f"{d.get('from_stage')} -> {d.get('to_stage')}",
                    f"event {d.get('event')}",
                    None,
                )
            )
        elif e.kind is EventKind.STATE:
            out.append(
                TimelineRow(
                    e.seq,
                    e.day.isoformat(),
                    e.actor.value,
                    f"stage {d.get('stage')}",
                    f"awaiting {d.get('awaiting')}; returned {d.get('returned_minor')} minor"
                    + ("; resolved" if d.get("resolved") else ""),
                    d.get("deadline_due"),
                )
            )
        elif e.kind is EventKind.CLOCK:
            out.append(
                TimelineRow(
                    e.seq,
                    e.day.isoformat(),
                    e.actor.value,
                    "clock",
                    f"{d.get('from')} -> {d.get('to')}",
                    None,
                )
            )
        elif e.kind is EventKind.INCIDENT:
            out.append(
                TimelineRow(
                    e.seq,
                    e.day.isoformat(),
                    e.actor.value,
                    f"incident {d.get('kind')}",
                    str(d.get("detail", "")),
                    None,
                )
            )
        elif e.kind is EventKind.CLOSE:
            out.append(
                TimelineRow(e.seq, e.day.isoformat(), e.actor.value, "close", _compact(d), None)
            )
    return out


def _documents(r: EpisodeRecord) -> list[DocumentRow]:
    original = {d.doc_id: d for d in r.original_documents}
    sources = {
        str(e.data.get("doc_id")): str(e.data.get("source", "env"))
        for e in r.events(EventKind.EVIDENCE_ADDED)
    }
    out = []
    for d in r.documents:
        o = original.get(d.doc_id)
        forged = r.case.truth.forged.get(d.doc_id)
        out.append(
            DocumentRow(
                doc_id=d.doc_id,
                kind=d.kind.value,
                issued_on=d.issued_on.isoformat(),
                content_hash=d.content_hash,
                field_names=tuple(sorted(d.fields)),
                source=sources.get(d.doc_id, "env"),
                changed=o is not None and o.content_hash != d.content_hash,
                forged=None if forged is None else forged.value,
            )
        )
    return out


def _link(ref: EvidenceRef, r: EpisodeRecord, docs: set[str]) -> tuple[str, str, str]:
    if ref.source == "trace":
        return (f"trace #{ref.ref}", f"#ev-{ref.ref}", ref.note)
    if ref.source == "posting":
        return (f"journal entry {ref.ref}", f"#je-{ref.ref}", ref.note)
    if ref.source == "document":
        href = f"#doc-{ref.ref}" if ref.ref in docs else ""
        return (f"document {ref.ref}", href, ref.note)
    if ref.source == "case":
        return (f"case {ref.ref}", "#case", ref.note)
    if ref.source == "claim":
        return (f"claim {ref.ref}", "#explanation", ref.note)
    if ref.source == "baseline":
        return (f"baseline {ref.ref}", "#baseline", ref.note)
    if ref.source == "episode":
        return (f"episode {ref.ref}", "#case" if ref.ref == r.episode_id else "", ref.note)
    return (f"{ref.source} {ref.ref}", "", ref.note)


def _balanced(e: JournalEntry) -> bool:
    return sum(p.amount_minor for p in e.postings) == 0


def case_view(record: EpisodeRecord, scorecard: Scorecard) -> CaseView:
    docs = {d.doc_id for d in record.documents}
    calls = tuple(
        CallRow(
            seq=e.seq,
            day=e.day.isoformat(),
            tool=e.tool or "?",
            args=_compact(e.args or {}),
            ok=e.ok,
            error=e.error,
            result=_compact(e.data),
        )
        for e in record.events(EventKind.TOOL_CALL)
    )
    broken = verify_events(record.trace)
    head_ok = (record.trace[-1].hash if record.trace else "0" * 64) == record.trace_head
    c = record.case
    case_fields = (
        ("case id", c.case_id),
        ("transaction", c.txn_id),
        ("merchant", f"{c.merchant_id} (mcc {c.mcc})"),
        ("network / reason code", f"{c.network.value} {c.reason_code}"),
        (
            "amount",
            f"{minor(c.amount_minor, c.currency)} (captured {minor(c.captured_minor, c.currency)})",
        ),
        ("settlement currency", c.settlement_currency),
        ("processed / opened", f"{c.processed_on.isoformat()} / {c.opened_on.isoformat()}"),
        ("opening stage", c.opening_stage.value),
        ("merchant tier", record.merchant_tier or "n/a"),
    )
    return CaseView(
        record=record,
        scorecard=scorecard,
        timeline=tuple(_timeline(record.trace)),
        journal=record.journal,
        journal_balanced=tuple(_balanced(e) for e in record.journal),
        documents=tuple(_documents(record)),
        calls=calls,
        checks=tuple(
            CheckRow(ch, tuple(_link(ref, record, docs) for ref in ch.evidence))
            for ch in scorecard.checks
        ),
        trace=record.trace,
        trace_broken=broken,
        trace_head_ok=head_ok,
        case_fields=case_fields,
    )


# --- diff between two agent versions (183) ---------------------------------------------------


@dataclass(frozen=True)
class TypeDelta:
    case_type: str
    n: int
    mean_a: float
    mean_b: float
    correct_a: int
    correct_b: int
    gate_a: int
    gate_b: int
    changed: int

    @property
    def delta(self) -> float:
        return self.mean_b - self.mean_a


@dataclass(frozen=True)
class ChangedCase:
    key: str  # episode id (agents) or case id (rules)
    case_type: str
    a: str  # action / resolution under A
    b: str
    cost_a: int
    cost_b: int

    @property
    def delta(self) -> int:
        return self.cost_b - self.cost_a


@dataclass(frozen=True)
class AgentDiff:
    a: AgentRun
    b: AgentRun
    paired: int
    unpaired: int
    types: tuple[TypeDelta, ...]
    changed: tuple[ChangedCase, ...]
    total_a: int
    total_b: int


def _outcome_key(r: EpisodeRecord) -> str:
    return f"{last_action(r)} / {r.resolution or ('lapsed' if r.lapsed else 'open')}"


def agent_diff(a: AgentRun, b: AgentRun) -> AgentDiff:
    rb = {r.episode_id: r for r in b.records}
    pairs = [(x, rb[x.episode_id]) for x in a.records if x.episode_id in rb]
    by_type: dict[str, list[tuple[EpisodeRecord, EpisodeRecord]]] = defaultdict(list)
    for x, y in pairs:
        by_type[x.case.truth.family.value].append((x, y))
    types = []
    for t in sorted(by_type):
        ps = by_type[t]
        types.append(
            TypeDelta(
                case_type=t,
                n=len(ps),
                mean_a=fmean([x.cost_usd_minor for x, _ in ps]),
                mean_b=fmean([y.cost_usd_minor for _, y in ps]),
                correct_a=sum(1 for x, _ in ps if x.outcome is not None and x.outcome.correct),
                correct_b=sum(1 for _, y in ps if y.outcome is not None and y.outcome.correct),
                gate_a=sum(1 for x, _ in ps if a.scorecards[x.episode_id].gate),
                gate_b=sum(1 for _, y in ps if b.scorecards[y.episode_id].gate),
                changed=sum(1 for x, y in ps if _outcome_key(x) != _outcome_key(y)),
            )
        )
    changed = sorted(
        (
            ChangedCase(
                x.episode_id,
                x.case.truth.family.value,
                _outcome_key(x),
                _outcome_key(y),
                x.cost_usd_minor,
                y.cost_usd_minor,
            )
            for x, y in pairs
            if _outcome_key(x) != _outcome_key(y)
        ),
        key=lambda c: (-abs(c.delta), c.key),
    )
    return AgentDiff(
        a=a,
        b=b,
        paired=len(pairs),
        unpaired=len(a.records) + len(b.records) - 2 * len(pairs),
        types=tuple(types),
        changed=tuple(changed),
        total_a=sum(x.cost_usd_minor for x, _ in pairs),
        total_b=sum(y.cost_usd_minor for _, y in pairs),
    )


# --- diff between two rule versions (183) ---------------------------------------------------


@dataclass(frozen=True)
class RuleStep:
    ref: str
    logic: str
    matched: bool
    chosen: bool


@dataclass(frozen=True)
class RuleTrace:
    """How one policy decided one case: every rule in priority order, then the money."""

    steps: tuple[RuleStep, ...]
    result: CaseResult
    journal: tuple[JournalEntry, ...]


@dataclass(frozen=True)
class RuleDiff:
    a: RuleVersion
    b: RuleVersion
    seed: int
    n: int
    types: tuple[TypeDelta, ...]
    changed: tuple[ChangedCase, ...]
    total_a: int
    total_b: int
    cases: dict[str, DisputeCase] = field(repr=False)
    runs: tuple[RunResult, RunResult] = field(repr=False)
    policies: tuple[Policy, Policy] = field(repr=False)

    def trace(self, case_id: str, side: int) -> RuleTrace | None:
        case = self.cases.get(case_id)
        if case is None:
            return None
        policy, run = self.policies[side], self.runs[side]
        view = case.view()
        result = next(r for r in run.results if r.case_id == case_id)
        steps = []
        chosen_seen = False
        for rule in policy.rules:
            hit = rule.spec.matches(view)
            chosen = hit and not chosen_seen
            chosen_seen = chosen_seen or hit
            steps.append(RuleStep(rule.ref, rule.spec.describe(), hit, chosen))
        journal = tuple(e for e in run.journal if e.case_id == case_id)
        return RuleTrace(tuple(steps), result, journal)


def _policy_with(gate: AgentGate, rv: RuleVersion) -> Policy:
    active = [r for r in gate.registry.active_rules() if r.rule_id != rv.rule_id]
    return policy_from(active).with_rule(as_policy_rule(rv))


def _rule_outcome(r: CaseResult) -> str:
    return f"{r.action.value} / {r.resolution.value}"


def rule_diff(gate: AgentGate, a: RuleVersion, b: RuleVersion, seed: int, n: int) -> RuleDiff:
    """Replay the live policy with `a`, then with `b`, on one public synthetic set.

    The replay set is seeded separately from the gate's held-out seed, so the cases a
    reviewer browses here are not the cases the gate decides on."""
    cases = generate_cases(seed, n, gate.config.profile, prefix="rp")
    pa, pb = _policy_with(gate, a), _policy_with(gate, b)
    sim = Simulator(cases)
    run_a = sim.run(pa)
    run_b = sim.run(pb)
    by_type: dict[str, list[tuple[CaseResult, CaseResult]]] = defaultdict(list)
    for x, y in zip(run_a.results, run_b.results, strict=True):
        by_type[x.scenario.value].append((x, y))
    types = tuple(
        TypeDelta(
            case_type=t,
            n=len(ps),
            mean_a=fmean([x.cost_usd_minor for x, _ in ps]),
            mean_b=fmean([y.cost_usd_minor for _, y in ps]),
            correct_a=sum(1 for x, _ in ps if x.regret_usd_minor == 0),
            correct_b=sum(1 for _, y in ps if y.regret_usd_minor == 0),
            gate_a=0,
            gate_b=0,
            changed=sum(1 for x, y in ps if _rule_outcome(x) != _rule_outcome(y)),
        )
        for t, ps in sorted(by_type.items())
    )
    changed = tuple(
        sorted(
            (
                ChangedCase(
                    x.case_id,
                    x.scenario.value,
                    _rule_outcome(x),
                    _rule_outcome(y),
                    x.cost_usd_minor,
                    y.cost_usd_minor,
                )
                for x, y in zip(run_a.results, run_b.results, strict=True)
                if _rule_outcome(x) != _rule_outcome(y)
            ),
            key=lambda c: (-abs(c.delta), c.key),
        )
    )
    return RuleDiff(
        a=a,
        b=b,
        seed=seed,
        n=n,
        types=types,
        changed=changed,
        total_a=sum(r.cost_usd_minor for r in run_a.results),
        total_b=sum(r.cost_usd_minor for r in run_b.results),
        cases={c.case_id: c for c in cases},
        runs=(run_a, run_b),
        policies=(pa, pb),
    )


# --- approvals inbox (184) ---------------------------------------------------------------------


@dataclass(frozen=True)
class PendingApproval:
    rv: RuleVersion
    canary_decision: DecisionRecord
    ramp: CanaryState | None
    actionable: bool  # canary passed and any ramp completed
    blocker: str
    request: ApprovalRequest | None
    status: ApprovalStatus | None


@dataclass(frozen=True)
class Inbox:
    policy: ApprovalPolicy | None
    single_approvers: tuple[str, ...]
    pending: tuple[PendingApproval, ...]
    history: tuple[AuditEntry, ...]


HISTORY_EVENTS = (*APPROVAL_EVENTS, EVENT_REJECTED, "rule.activated")


def inbox(gate: AgentGate, now: datetime) -> Inbox:
    entries = gate.audit.entries()
    policy = project_policy(entries)
    pending = []
    for rv in gate.registry.all_versions():
        if rv.status is not RuleStatus.CANARY:
            continue
        canary = [
            r
            for r in gate.decisions(rv.rule_id)
            if r.decision.version == rv.version and r.decision.stage is Stage.CANARY
        ]
        if not canary or canary[-1].decision.outcome is not Outcome.CANARY_PASSED:
            continue
        ramp = gate.canary_state(rv.rule_id, rv.version)
        actionable = ramp is None or ramp.status == "completed"
        blocker = "" if actionable else f"canary ramp is {ramp.status if ramp else 'missing'}"
        request = status = None
        if policy is not None:
            request, grants = project_approvals(entries, rv.rule_id, rv.version)
            status = evaluate_approvals(policy, request, grants, now)
        pending.append(PendingApproval(rv, canary[-1], ramp, actionable, blocker, request, status))
    history = tuple(e for e in entries if e.event_type in HISTORY_EVENTS)[-30:][::-1]
    return Inbox(policy, gate.config.approvers, tuple(pending), history)


def payload_text(e: AuditEntry) -> str:
    p = e.payload
    keys = ("rule_id", "version", "proposer", "approver", "reason", "decision_id")
    parts = [f"{k}={p[k]}" for k in keys if k in p]
    if e.event_type == "approval.policy_set":
        pol = p.get("policy", {})
        parts.append(f"required={pol.get('required')} of {len(pol.get('approvers', []))}")
    return " ".join(parts)


def counts(values: Iterable[str]) -> list[tuple[str, int]]:
    return sorted(Counter(values).items())


__all__ = [
    "AgentDiff",
    "AgentSummary",
    "CaseView",
    "Inbox",
    "RuleDiff",
    "VersionDetail",
    "VersionRow",
    "agent_diff",
    "agent_summary",
    "case_view",
    "episode_rows",
    "inbox",
    "rule_diff",
    "version_detail",
    "version_rows",
]
