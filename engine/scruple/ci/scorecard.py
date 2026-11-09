"""The CI scorecard: one JSON document per gated candidate.

Built only from stored decision records, so it says nothing the audit trail does
not. No timestamps: two runs on the same inputs give byte-identical scorecards.
All numbers come from the synthetic simulator and are labelled so.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from scruple.audit.report import usd
from scruple.gate.config import GateConfig
from scruple.gate.models import CheckResult, DecisionRecord, Outcome, Stage
from scruple.hashing import content_hash

SCHEMA_VERSION = 1


class Verdict(StrEnum):
    PASS = "pass"  # noqa: S105 - not a password
    HOLD = "hold"
    BLOCK = "block"


class DecisionLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str
    stage: Stage
    outcome: Outcome
    summary: str
    reasons: tuple[str, ...]
    decision_sha256: str


class Regression(BaseModel):
    """Something the candidate made worse. `kind` is check, slice or golden."""

    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    detail: str


class SliceLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario: str
    n: int
    baseline_mean_regret: float
    candidate_mean_regret: float
    delta: float
    changed_cases: int


class Statistics(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: str
    confidence: float
    mean_improvement_usd_minor: float
    ci_low: float
    ci_high: float
    n_cases: int
    affected_cases: int


class CanaryLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    fraction: float
    matched_treatment: int
    matched_control: int
    treatment_mean_cost_usd_minor: float
    control_mean_cost_usd_minor: float


class Scorecard(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SCHEMA_VERSION
    data: str = "synthetic"
    verdict: Verdict
    exit_code: int
    rule_id: str
    version: int
    content_hash: str
    rule_logic: str
    rule_description: str
    decisions: tuple[DecisionLine, ...]
    checks: tuple[CheckResult, ...]
    regressions: tuple[Regression, ...]
    slices: tuple[SliceLine, ...]
    statistics: Statistics | None
    canary: CanaryLine | None
    config_sha256: str
    audit_head: str

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def verdict_of(records: Sequence[DecisionRecord]) -> Verdict:
    last = records[-1].decision.outcome
    if last in (Outcome.PROMOTE, Outcome.CANARY_PASSED, Outcome.ACTIVATED, Outcome.HEALTHY):
        return Verdict.PASS
    if last is Outcome.HOLD:
        return Verdict.HOLD
    return Verdict.BLOCK


def exit_code_for(verdict: Verdict, fail_on_hold: bool) -> int:
    if verdict is Verdict.BLOCK:
        return 1
    if verdict is Verdict.HOLD and fail_on_hold:
        return 1
    return 0


def build_scorecard(
    records: Sequence[DecisionRecord],
    config: GateConfig,
    audit_head: str,
    fail_on_hold: bool = True,
    method: str = "percentile-bootstrap-paired",
) -> Scorecard:
    if not records:
        raise ValueError("a scorecard needs at least one decision")
    first = records[0].decision
    verdict = verdict_of(records)
    checks = tuple(c for r in records for c in r.decision.checks)
    shadow = next((r.decision.shadow for r in records if r.decision.shadow), None)
    canary = next((r.decision.canary for r in records if r.decision.canary), None)

    regressions: list[Regression] = [
        Regression(kind="check", name=c.name, detail=c.detail)
        for c in checks
        if c.kind == "hard" and not c.passed
    ]
    slices: list[SliceLine] = []
    stats: Statistics | None = None
    if shadow is not None:
        for s in shadow.slices:
            slices.append(
                SliceLine(
                    scenario=s.scenario,
                    n=s.n,
                    baseline_mean_regret=s.baseline_mean_regret,
                    candidate_mean_regret=s.candidate_mean_regret,
                    delta=s.delta,
                    changed_cases=s.changed_cases,
                )
            )
            if s.delta > config.slice_tolerance_usd_minor:
                regressions.append(
                    Regression(
                        kind="slice",
                        name=s.scenario,
                        detail=f"mean regret worse by {usd(s.delta)} per case over {s.n} cases",
                    )
                )
        regressions += [
            Regression(kind="golden", name=g, detail="golden case regressed")
            for g in shadow.golden_regressions
        ]
        stats = Statistics(
            method=method,
            confidence=shadow.confidence,
            mean_improvement_usd_minor=shadow.mean_improvement_usd_minor,
            ci_low=shadow.ci_low,
            ci_high=shadow.ci_high,
            n_cases=shadow.n_cases,
            affected_cases=shadow.affected_cases,
        )
    canary_line = (
        None
        if canary is None
        else CanaryLine(
            fraction=canary.fraction,
            matched_treatment=canary.matched_treatment,
            matched_control=canary.matched_control,
            treatment_mean_cost_usd_minor=canary.treatment_mean_cost_usd_minor,
            control_mean_cost_usd_minor=canary.control_mean_cost_usd_minor,
        )
    )
    return Scorecard(
        verdict=verdict,
        exit_code=exit_code_for(verdict, fail_on_hold),
        rule_id=first.rule_id,
        version=first.version,
        content_hash=first.content_hash,
        rule_logic=first.rule_logic,
        rule_description=first.rule_description,
        decisions=tuple(
            DecisionLine(
                decision_id=r.decision.decision_id,
                stage=r.decision.stage,
                outcome=r.decision.outcome,
                summary=r.decision.summary,
                reasons=r.decision.reasons,
                decision_sha256=r.decision_sha256,
            )
            for r in records
        ),
        checks=checks,
        regressions=tuple(regressions),
        slices=tuple(slices),
        statistics=stats,
        canary=canary_line,
        config_sha256=content_hash(config),
        audit_head=audit_head,
    )


def load_scorecard(text: str) -> Scorecard:
    return Scorecard.model_validate_json(text)
