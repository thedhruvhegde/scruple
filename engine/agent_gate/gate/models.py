"""Decision records: everything the evidence report and audit trail need."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent_gate.verifiers.base import Finding


class Stage(StrEnum):
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVATION = "activation"
    ROLLBACK = "rollback"
    ASSURANCE = "assurance"


class Outcome(StrEnum):
    PROMOTE = "promote"  # shadow passed, go to canary
    HOLD = "hold"  # not enough evidence either way
    BLOCK = "block"  # a hard check failed
    CANARY_PASSED = "canary_passed"  # ready for human approval
    ROLLBACK = "rollback"  # canary or assurance regression, rolled back
    ACTIVATED = "activated"
    ROLLED_BACK = "rolled_back"
    HEALTHY = "healthy"


CheckKind = Literal["hard", "evidence", "info"]


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    kind: CheckKind
    passed: bool
    detail: str


class ArmStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    mean_regret_usd_minor: float
    accuracy: float
    escalation_rate: float
    ledger_findings: int
    policy_findings: int


class SliceStat(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario: str
    n: int
    baseline_mean_regret: float
    candidate_mean_regret: float
    changed_cases: int

    @property
    def delta(self) -> float:
        return self.candidate_mean_regret - self.baseline_mean_regret


class ConflictStat(BaseModel):
    model_config = ConfigDict(frozen=True)

    overridden_rule: str
    overridden_action: str
    cases: int


class ShadowMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    n_cases: int
    eval_seed_commitment: str
    profile: str
    affected_cases: int
    matched_cases: int
    baseline: ArmStats
    candidate: ArmStats
    mean_improvement_usd_minor: float
    ci_low: float
    ci_high: float
    confidence: float
    slices: tuple[SliceStat, ...]
    golden_cases: int
    golden_regressions: tuple[str, ...]
    conflicts: tuple[ConflictStat, ...]
    new_ledger_findings: tuple[Finding, ...]
    new_policy_findings: tuple[Finding, ...]
    baseline_rules: tuple[str, ...]


class CanaryMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    live_cases: int
    fraction: float
    profile: str
    treatment_cases: int
    matched_treatment: int
    matched_control: int
    treatment_mean_cost_usd_minor: float
    control_mean_cost_usd_minor: float
    new_hard_findings: tuple[Finding, ...]


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str
    stage: Stage
    outcome: Outcome
    rule_id: str
    version: int
    content_hash: str
    rule_description: str
    rule_logic: str
    owner: str
    agent_id: str
    episodes: int
    actor: str
    created_at: datetime
    summary: str
    reasons: tuple[str, ...]
    checks: tuple[CheckResult, ...] = ()
    shadow: ShadowMetrics | None = None
    canary: CanaryMetrics | None = None
    approvers: tuple[str, ...] = ()


class DecisionRecord(BaseModel):
    """A decision plus where it is anchored in the audit chain."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    decision_sha256: str
    audit_seq: int
    audit_entry_hash: str
