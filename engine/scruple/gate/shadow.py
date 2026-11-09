"""Shadow mode: score live decisions without acting on them.

The production agent sends each decision it made (the case plus the action it
took). The recorder replays the batch twice in a private simulator, once with the
production actions and once with the candidate policy, runs the verifiers on both,
and accumulates per-case-type statistics.

Zero side effects, by construction:
- this module imports nothing that can write: no storage, registry, audit log or
  service (tests check the import graph);
- the recorder holds only immutable rule snapshots and in-memory counters;
- every replay uses a fresh `Simulator`, whose ledger is private to that replay;
- `record` returns statistics only. It never returns an action to execute.

Case type is the observable `reason_code`. Ground truth (needed for regret) exists
only because the cases are synthetic, so `record` and `summary` are synthetic.

Real cases: `record_imported` takes decisions production made on imported cases
(`ExternalCase`, no ground truth) and `observe_imported` their outcomes when they
arrive; `summary_imported` scores what is observable and labels every metric observed
or estimated (`scruple.gate.shadow_imported`). The candidate there is this rule alone,
judged where it fires: elsewhere it keeps production's action, so a difference is
always the candidate's doing (the active rules are not replayed against production).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from scruple.backtest.candidate import RuleCandidate, RuleEntry
from scruple.domain import Action
from scruple.gate.provider import Interval, StatsProvider, default_provider
from scruple.gate.shadow_imported import (
    ImportedBatchResult,
    ImportedDecision,
    ImportedObserveResult,
    ImportedOutcome,
    ImportedShadowRecorder,
    ImportedShadowSummary,
)
from scruple.registry.models import RuleVersion
from scruple.sim.engine import RunResult, Simulator
from scruple.sim.models import DisputeCase
from scruple.sim.policy import Policy, PolicyRule
from scruple.verifiers.costs import realized_costs
from scruple.verifiers.ledger import verify_ledger
from scruple.verifiers.policy_constraints import verify_policy

MAX_BATCH = 10_000


class LiveDecision(BaseModel):
    """One decision the production agent already made. Input only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case: DisputeCase
    action: Action
    agent_id: str = Field(default="production", pattern=r"^[A-Za-z0-9._@:-]{1,128}$")


class LiveDecisionBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: tuple[LiveDecision, ...] = Field(min_length=1, max_length=MAX_BATCH)


class CaseTypeStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_type: str
    n: int
    agree: int
    candidate_decided: int
    production_mean_cost_usd_minor: float
    candidate_mean_cost_usd_minor: float
    production_mean_regret_usd_minor: float
    candidate_mean_regret_usd_minor: float
    production_hard_findings: int
    candidate_hard_findings: int


class ShadowSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: str = "synthetic"
    rule_ref: str
    content_hash: str
    n: int
    agree: int
    production_hard_findings: int
    candidate_hard_findings: int
    regret_improvement: Interval  # production regret minus candidate regret, per case
    by_case_type: tuple[CaseTypeStats, ...]


class BatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    recorded: int
    agree: int
    candidate_hard_findings: int
    total_recorded: int


@dataclass
class _Acc:
    n: int = 0
    agree: int = 0
    decided: int = 0
    p_cost: int = 0
    c_cost: int = 0
    p_regret: int = 0
    c_regret: int = 0
    p_hard: int = 0
    c_hard: int = 0


@dataclass
class _State:
    seen: set[str] = field(default_factory=set)
    diffs: list[int] = field(default_factory=list)
    by_type: dict[str, _Acc] = field(default_factory=lambda: defaultdict(_Acc))


def _hard_by_case(cases: Sequence[DisputeCase], run: RunResult) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for f in (*verify_ledger(cases, run).findings, *verify_policy(cases, run).findings):
        out[f.case_id] += 1
    return out


class ShadowRecorder:
    def __init__(
        self,
        candidate: RuleVersion,
        active: Sequence[RuleVersion],
        provider: StatsProvider | None = None,
        confidence: float = 0.95,
    ) -> None:
        # Snapshots: RuleVersion is frozen, and we keep our own tuple.
        self.candidate = candidate
        self.active = tuple(active)
        baseline = Policy(rules=tuple(PolicyRule(r.rule_id, r.ref, r.spec) for r in self.active))
        self._policy = baseline.with_rule(
            PolicyRule(candidate.rule_id, candidate.ref, candidate.spec)
        )
        self._provider = provider or default_provider()
        self._confidence = confidence
        self._state = _State()
        self._imported: ImportedShadowRecorder | None = None

    # --- imported (real) cases ---------------------------------------------------

    def imported(self) -> ImportedShadowRecorder:
        """The recorder for imported cases (created on first use; in memory only)."""
        if self._imported is None:
            rule = RuleCandidate(
                (RuleEntry(rule_id=self.candidate.rule_id, spec=self.candidate.spec),),
                default=None,
                name=self.candidate.ref,
            )
            self._imported = ImportedShadowRecorder(rule)
        return self._imported

    def record_imported(self, decisions: Sequence[ImportedDecision]) -> ImportedBatchResult:
        return self.imported().record(decisions)

    def observe_imported(self, outcomes: Sequence[ImportedOutcome]) -> ImportedObserveResult:
        return self.imported().observe(outcomes)

    def summary_imported(self) -> ImportedShadowSummary:
        return self.imported().summary()

    def record(self, decisions: Sequence[LiveDecision]) -> BatchResult:
        """Replay a batch. Rejects the whole batch if any case was already recorded
        (or repeats within the batch), so statistics never double count."""
        if not decisions:
            raise ValueError("empty batch")
        if len(decisions) > MAX_BATCH:
            raise ValueError(f"batch larger than {MAX_BATCH}")
        ids = [d.case.case_id for d in decisions]
        dup = sorted({i for i in ids if i in self._state.seen} | _repeats(ids))
        if dup:
            raise ValueError(f"already recorded: {', '.join(dup[:5])}")
        cases = [d.case for d in decisions]
        prod_action = {d.case.case_id: d.action for d in decisions}
        fixed = {a: Policy(rules=(), default_action=a) for a in Action}
        sim = Simulator(cases)
        p_run = sim.run_routed(lambda c: fixed[prod_action[c.case_id]])
        c_run = sim.run(self._policy)
        p_cost, c_cost = realized_costs(p_run), realized_costs(c_run)
        p_hard, c_hard = _hard_by_case(cases, p_run), _hard_by_case(cases, c_run)
        agree = 0
        cand_hard = 0
        for case, pr, cr in zip(cases, p_run.results, c_run.results, strict=True):
            i = case.case_id
            acc = self._state.by_type[case.reason_code.value]
            same = pr.action is cr.action
            agree += same
            cand_hard += c_hard[i]
            acc.n += 1
            acc.agree += same
            acc.decided += cr.decided_by == self.candidate.ref
            acc.p_cost += p_cost[i]
            acc.c_cost += c_cost[i]
            acc.p_regret += pr.regret_usd_minor
            acc.c_regret += cr.regret_usd_minor
            acc.p_hard += p_hard[i]
            acc.c_hard += c_hard[i]
            self._state.diffs.append(pr.regret_usd_minor - cr.regret_usd_minor)
        self._state.seen.update(ids)
        return BatchResult(
            recorded=len(cases),
            agree=agree,
            candidate_hard_findings=cand_hard,
            total_recorded=len(self._state.seen),
        )

    def summary(self) -> ShadowSummary:
        rows = []
        for t in sorted(self._state.by_type):
            a = self._state.by_type[t]
            rows.append(
                CaseTypeStats(
                    case_type=t,
                    n=a.n,
                    agree=a.agree,
                    candidate_decided=a.decided,
                    production_mean_cost_usd_minor=a.p_cost / a.n,
                    candidate_mean_cost_usd_minor=a.c_cost / a.n,
                    production_mean_regret_usd_minor=a.p_regret / a.n,
                    candidate_mean_regret_usd_minor=a.c_regret / a.n,
                    production_hard_findings=a.p_hard,
                    candidate_hard_findings=a.c_hard,
                )
            )
        return ShadowSummary(
            rule_ref=self.candidate.ref,
            content_hash=self.candidate.content_hash,
            n=sum(r.n for r in rows),
            agree=sum(r.agree for r in rows),
            production_hard_findings=sum(r.production_hard_findings for r in rows),
            candidate_hard_findings=sum(r.candidate_hard_findings for r in rows),
            regret_improvement=self._provider.paired_interval(self._state.diffs, self._confidence),
            by_case_type=tuple(rows),
        )


def _repeats(ids: Sequence[str]) -> set[str]:
    seen: set[str] = set()
    rep: set[str] = set()
    for i in ids:
        if i in seen:
            rep.add(i)
        seen.add(i)
    return rep
