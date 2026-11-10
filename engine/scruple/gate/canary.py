"""Staged canary: deterministic traffic assignment, a ramp schedule, stage evaluation.

Pure. `AgentGate` (service.py) applies stage results and persists ramp progress as
audit events, so the ramp survives restarts and every step is in the hash chain.
`project_state` rebuilds the ramp from those events.

Assignment hashes `case_id|salt` into one of 10,000 buckets (the same formula as
`engine.in_canary`) and sends bucket < percent x 100 to the candidate. So:
- stable: a case always lands in the same arm for a given salt and percent;
- monotone: raising the percent only moves cases from baseline to candidate;
- the salt is the candidate's content hash, so different candidates get
  independent slices.

A ramp never reaches 100%: the last stage needs a control group to compare
against. Going to 100% is activation, which needs human approval.

Breach rule (same as the one-shot canary): hard ledger/policy violations on cases
the candidate decided, or treatment mean realized cost above control by more than
the tolerance. The interval from the injected `StatsProvider` is reported with
every stage, so a sequential test can replace the point rule later without
changing callers.

With a `GateConfig` whose `stats` is "evalue", the point rule is replaced by the
anytime-valid two-sample test in `scruple.gate.sequential` (inverse-propensity
betting on the known assignment probability). Each stage is its own sequential
test on fresh live cases at level `evalue_alpha / len(stages)`, so the chance of a
false rollback anywhere in the ramp is at most `evalue_alpha` (union bound).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scruple.audit.models import AuditEntry
from scruple.gate.config import GateConfig
from scruple.gate.models import CanaryMetrics, CheckResult, Outcome
from scruple.gate.provider import Interval, StatsProvider, default_provider
from scruple.gate.sequential import (
    SequentialCanaryResult,
    describe,
    observations,
    sequential_checks,
)
from scruple.hashing import sha256_hex
from scruple.registry.models import RuleVersion
from scruple.sim.engine import Simulator
from scruple.sim.models import DisputeCase
from scruple.sim.policy import Policy, PolicyRule
from scruple.verifiers.base import Finding
from scruple.verifiers.costs import realized_costs
from scruple.verifiers.ledger import verify_ledger
from scruple.verifiers.policy_constraints import verify_policy

BUCKETS = 10_000

EVENT_STARTED = "canary.ramp_started"
EVENT_STAGE_PASSED = "canary.stage_passed"
EVENT_STAGE_HELD = "canary.stage_held"
EVENT_COMPLETED = "canary.ramp_completed"
EVENT_BREACHED = "canary.breached"
CANARY_EVENTS = (
    EVENT_STARTED,
    EVENT_STAGE_PASSED,
    EVENT_STAGE_HELD,
    EVENT_COMPLETED,
    EVENT_BREACHED,
)


class Arm(StrEnum):
    CANDIDATE = "candidate"
    BASELINE = "baseline"


def bucket(case_id: str, salt: str) -> int:
    return int(sha256_hex(f"{case_id}|{salt}")[:8], 16) % BUCKETS


def basis_points(percent: float) -> int:
    if not 0.0 <= percent <= 100.0:
        raise ValueError(f"percent must be within [0, 100], got {percent}")
    return round(percent * BUCKETS / 100)


def assign(case_id: str, salt: str, percent: float) -> Arm:
    return Arm.CANDIDATE if bucket(case_id, salt) < basis_points(percent) else Arm.BASELINE


class RampSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stages: tuple[float, ...] = (5.0, 10.0, 25.0, 50.0)
    min_matched: int = Field(default=20, ge=1)
    tolerance_usd_minor: float = Field(default=500.0, ge=0.0)
    confidence: float = Field(default=0.95, gt=0.5, lt=1.0)

    @field_validator("stages")
    @classmethod
    def _increasing(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if not v:
            raise ValueError("a ramp needs at least one stage")
        if any(not 0.0 < p < 100.0 for p in v):
            raise ValueError("stages must be strictly between 0 and 100 percent")
        if any(b <= a for a, b in pairwise(v)):
            raise ValueError("stages must be strictly increasing")
        if len({basis_points(p) for p in v}) != len(v):
            raise ValueError("stages must differ by at least 0.01 percent")
        return v


StageOutcome = Literal["advance", "complete", "hold", "breach"]


class StageResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage_index: int
    percent: float
    outcome: StageOutcome
    live_cases: int
    treatment_cases: int
    matched_treatment: int
    matched_control: int
    treatment_mean_cost_usd_minor: float
    control_mean_cost_usd_minor: float
    cost_difference: Interval  # treatment minus control, realized cost per case
    hard_findings: tuple[Finding, ...]
    checks: tuple[CheckResult, ...]
    summary: str
    reasons: tuple[str, ...]
    sequential: SequentialCanaryResult | None = None

    @property
    def gate_outcome(self) -> Outcome:
        return {
            "advance": Outcome.PROMOTE,
            "complete": Outcome.CANARY_PASSED,
            "hold": Outcome.HOLD,
            "breach": Outcome.ROLLBACK,
        }[self.outcome]

    def metrics(self, profile: str) -> CanaryMetrics:
        return CanaryMetrics(
            live_cases=self.live_cases,
            fraction=self.percent / 100,
            profile=profile,
            treatment_cases=self.treatment_cases,
            matched_treatment=self.matched_treatment,
            matched_control=self.matched_control,
            treatment_mean_cost_usd_minor=self.treatment_mean_cost_usd_minor,
            control_mean_cost_usd_minor=self.control_mean_cost_usd_minor,
            new_hard_findings=self.hard_findings[:10],
            sequential=self.sequential,
        )


def _check(name: str, kind: str, passed: bool, detail: str) -> CheckResult:
    return CheckResult.model_validate(
        {"name": name, "kind": kind, "passed": passed, "detail": detail}
    )


def _usd(minor: float) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}${abs(minor) / 100:,.2f}"


def evaluate_stage(
    candidate: RuleVersion,
    active: Sequence[RuleVersion],
    cases: Sequence[DisputeCase],
    schedule: RampSchedule,
    stage_index: int,
    provider: StatsProvider | None = None,
    config: GateConfig | None = None,
) -> StageResult:
    if not 0 <= stage_index < len(schedule.stages):
        raise ValueError(f"stage {stage_index} is outside the schedule")
    if not cases:
        raise ValueError("a canary stage needs live cases")
    provider = provider or default_provider()
    percent = schedule.stages[stage_index]
    baseline = Policy(rules=tuple(PolicyRule(r.rule_id, r.ref, r.spec) for r in active))
    cand = baseline.with_rule(PolicyRule(candidate.rule_id, candidate.ref, candidate.spec))
    treat = {
        c.case_id
        for c in cases
        if assign(c.case_id, candidate.content_hash, percent) is Arm.CANDIDATE
    }
    run = Simulator(cases).run_routed(lambda c: cand if c.case_id in treat else baseline)
    costs = realized_costs(run)
    matches = {c.case_id for c in cases if cand.decide(c.view())[1] == candidate.ref}
    decided = sorted(r.case_id for r in run.results if r.decided_by == candidate.ref)
    control = sorted(matches - treat)
    t_costs = [float(costs[i]) for i in decided]
    c_costs = [float(costs[i]) for i in control]
    t_mean = sum(t_costs) / len(t_costs) if t_costs else 0.0
    c_mean = sum(c_costs) / len(c_costs) if c_costs else 0.0
    diff = provider.two_sample_interval(t_costs, c_costs, schedule.confidence)
    decided_set = set(decided)
    hard = tuple(
        f
        for f in (*verify_ledger(cases, run).findings, *verify_policy(cases, run).findings)
        if f.case_id in decided_set
    )
    enough = len(decided) >= schedule.min_matched and len(control) >= schedule.min_matched
    within = t_mean <= c_mean + schedule.tolerance_usd_minor
    seq: SequentialCanaryResult | None = None
    cost_checks: tuple[CheckResult, ...]
    if config is not None and config.stats in ("evalue", "evalue_vr"):
        cost_checks, seq = sequential_checks(
            observations(cases, matches, treat, costs, config),
            basis_points(percent) / BUCKETS,
            config.evalue_alpha / len(schedule.stages),
            schedule.tolerance_usd_minor,
            centred=config.stats == "evalue_vr",
        )
    else:
        cost_checks = (
            _check(
                "canary_cost_within_tolerance",
                "hard",
                within or not enough,
                f"treatment mean cost {_usd(t_mean)} vs control {_usd(c_mean)}, difference "
                f"{_usd(diff.mean)} ({diff.confidence:.0%} interval {_usd(diff.low)} to "
                f"{_usd(diff.high)}, {diff.method}; "
                f"tolerance {_usd(schedule.tolerance_usd_minor)})",
            ),
        )
    checks = (
        _check(
            "canary_no_hard_violations",
            "hard",
            not hard,
            f"{len(hard)} ledger/policy violations on canary cases",
        ),
        *cost_checks,
        _check(
            "canary_enough_traffic",
            "evidence",
            enough,
            f"{len(decided)} treatment / {len(control)} control matched cases "
            f"(minimum {schedule.min_matched} each)",
        ),
    )
    failed_hard = [c for c in checks if c.kind == "hard" and not c.passed]
    last = stage_index == len(schedule.stages) - 1
    outcome: StageOutcome
    if failed_hard:
        outcome = "breach"
        summary = f"Canary breach at {percent:g}%: automatically rolled back."
        reasons = tuple(f"{c.name}: {c.detail}" for c in failed_hard)
    elif not enough:
        outcome = "hold"
        summary = f"Canary stage {percent:g}% inconclusive: not enough live traffic yet."
        reasons = tuple(f"{c.name}: {c.detail}" for c in checks if not c.passed)
    elif last:
        outcome = "complete"
        summary = f"Canary ramp complete at {percent:g}%: ready for human approval."
        reasons = ("live outcomes within tolerance of control", "no hard violations")
    else:
        nxt = schedule.stages[stage_index + 1]
        outcome = "advance"
        summary = f"Canary stage {percent:g}% passed: ramping to {nxt:g}%."
        reasons = ("live outcomes within tolerance of control", "no hard violations")
    if seq is not None and outcome in ("complete", "advance"):
        reasons = (describe(seq), "no hard violations")
    return StageResult(
        stage_index=stage_index,
        percent=percent,
        outcome=outcome,
        live_cases=len(cases),
        treatment_cases=len(treat),
        matched_treatment=len(decided),
        matched_control=len(control),
        treatment_mean_cost_usd_minor=t_mean,
        control_mean_cost_usd_minor=c_mean,
        cost_difference=diff,
        hard_findings=hard,
        checks=checks,
        summary=summary,
        reasons=reasons,
        sequential=seq,
    )


RampStatus = Literal["ramping", "completed", "breached"]


class CanaryState(BaseModel):
    """Ramp progress, rebuilt from audit events (never stored anywhere else)."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    version: int
    content_hash: str
    schedule: RampSchedule
    stage_index: int
    status: RampStatus
    started_seq: int
    last_seq: int

    @property
    def percent(self) -> float:
        """Traffic share the candidate receives right now."""
        if self.status == "breached":
            return 0.0
        return self.schedule.stages[self.stage_index]


def project_state(entries: Sequence[AuditEntry], rule_id: str, version: int) -> CanaryState | None:
    state: CanaryState | None = None
    for e in entries:
        if e.event_type not in CANARY_EVENTS:
            continue
        p: dict[str, Any] = e.payload
        if p.get("rule_id") != rule_id or int(p.get("version", -1)) != version:
            continue
        if e.event_type == EVENT_STARTED:
            state = CanaryState(
                rule_id=rule_id,
                version=version,
                content_hash=p["content_hash"],
                schedule=RampSchedule.model_validate(p["schedule"]),
                stage_index=0,
                status="ramping",
                started_seq=e.seq,
                last_seq=e.seq,
            )
            continue
        if state is None:
            continue
        update: dict[str, Any] = {"last_seq": e.seq}
        if e.event_type == EVENT_STAGE_PASSED:
            update["stage_index"] = int(p["next_stage_index"])
        elif e.event_type == EVENT_COMPLETED:
            update["status"] = "completed"
        elif e.event_type == EVENT_BREACHED:
            update["status"] = "breached"
        state = state.model_copy(update=update)
    return state
