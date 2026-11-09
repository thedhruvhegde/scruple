"""Weighted checks with evidence, and the scorecard that combines them (061).

Composition (docs/research/VERIFIERS.md section 7.1)::

    gate(ep)    = AND over every hard check h: h(ep) passed
    quality(ep) = sum_i w_i * s_i(ep) / sum_i w_i          s_i in [0, 1]
    reward(ep)  = gate(ep) * quality(ep)

A hard failure zeroes the reward: an agent cannot buy back a violation with volume
on the soft checks. Every check emits `{id, passed, score, evidence, explanation}`,
where evidence points at trace events (by sequence number), journal entries,
documents, the case or the agent's claims. Hard checks name the exploits (X-ids in
the research notes) they guard against.

Per-episode checks implement `Check.run(record)`. Checks over a batch of episodes
(consistency, calibration, batch self-report) implement `BatchCheck.run_batch`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field

from scruple.verifiers.base import VerifierReport
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.trace import EventKind, verify_events

_FROZEN = ConfigDict(frozen=True, extra="forbid")

Source = Literal["trace", "posting", "document", "case", "claim", "baseline", "episode", "run"]


class EvidenceRef(BaseModel):
    model_config = _FROZEN

    source: Source
    ref: str  # trace seq, journal entry id, doc id, field name, episode id
    note: str = ""


def at(seq: int, note: str = "") -> EvidenceRef:
    return EvidenceRef(source="trace", ref=str(seq), note=note)


class CheckResult(BaseModel):
    model_config = _FROZEN

    id: str
    name: str
    weight: float = Field(ge=0)
    hard: bool
    passed: bool
    score: float = Field(ge=0, le=1)
    evidence: tuple[EvidenceRef, ...] = ()
    explanation: str = ""
    guards: tuple[str, ...] = ()  # exploit ids this check is meant to catch


class Check(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def weight(self) -> float: ...
    @property
    def hard(self) -> bool: ...
    @property
    def guards(self) -> tuple[str, ...]: ...

    def run(self, record: EpisodeRecord) -> CheckResult: ...


class BatchCheck(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def weight(self) -> float: ...
    @property
    def hard(self) -> bool: ...
    @property
    def guards(self) -> tuple[str, ...]: ...

    def run_batch(self, records: Sequence[EpisodeRecord]) -> CheckResult: ...


Verdict = tuple[bool, float, Sequence[EvidenceRef], str]  # passed, score, evidence, why


@dataclass(frozen=True)
class FnCheck:
    """A check from a function `record -> (passed, score, evidence, explanation)`.

    For a hard check the score is 1 when it passes and 0 when it fails."""

    id: str
    name: str
    fn: Callable[[EpisodeRecord], Verdict]
    weight: float = 1.0
    hard: bool = False
    guards: tuple[str, ...] = ()

    def run(self, record: EpisodeRecord) -> CheckResult:
        passed, score, evidence, why = self.fn(record)
        if self.hard:
            score = 1.0 if passed else 0.0
        return CheckResult(
            id=self.id,
            name=self.name,
            weight=self.weight,
            hard=self.hard,
            passed=passed,
            score=min(1.0, max(0.0, score)),
            evidence=tuple(evidence),
            explanation=why,
            guards=self.guards,
        )


@dataclass(frozen=True)
class FnBatchCheck:
    id: str
    name: str
    fn: Callable[[Sequence[EpisodeRecord]], Verdict]
    weight: float = 1.0
    hard: bool = False
    guards: tuple[str, ...] = ()

    def run_batch(self, records: Sequence[EpisodeRecord]) -> CheckResult:
        passed, score, evidence, why = self.fn(records)
        if self.hard:
            score = 1.0 if passed else 0.0
        return CheckResult(
            id=self.id,
            name=self.name,
            weight=self.weight,
            hard=self.hard,
            passed=passed,
            score=min(1.0, max(0.0, score)),
            evidence=tuple(evidence),
            explanation=why,
            guards=self.guards,
        )


def _quality(checks: Sequence[CheckResult]) -> float:
    total = sum(c.weight for c in checks)
    return sum(c.weight * c.score for c in checks) / total if total else 0.0


class Scorecard(BaseModel):
    model_config = _FROZEN

    episode_id: str
    checks: tuple[CheckResult, ...]
    synthetic: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gate(self) -> bool:
        return all(c.passed for c in self.checks if c.hard)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quality(self) -> float:
        return _quality(self.checks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reward(self) -> float:
        return self.quality if self.gate else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hard_failures(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.checks if c.hard and not c.passed)

    @property
    def failed(self) -> tuple[str, ...]:
        """Every check that did not pass, hard or soft."""
        return tuple(c.id for c in self.checks if not c.passed)

    def get(self, check_id: str) -> CheckResult:
        for c in self.checks:
            if c.id == check_id:
                return c
        raise KeyError(check_id)

    def summary(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "quality": round(self.quality, 4),
            "reward": round(self.reward, 4),
            "hard_failures": list(self.hard_failures),
            "failed": list(self.failed),
        }


class BatchScorecard(BaseModel):
    model_config = _FROZEN

    episodes: tuple[Scorecard, ...]
    checks: tuple[CheckResult, ...]  # batch-level checks
    synthetic: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gate(self) -> bool:
        return all(c.passed for c in self.checks if c.hard) and all(e.gate for e in self.episodes)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def episode_gate_rate(self) -> float:
        return sum(e.gate for e in self.episodes) / len(self.episodes) if self.episodes else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mean_reward(self) -> float:
        if not self.episodes:
            return 0.0
        batch_ok = all(c.passed for c in self.checks if c.hard)
        mean = sum(e.reward for e in self.episodes) / len(self.episodes)
        return mean if batch_ok else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hard_failures(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.checks if c.hard and not c.passed)

    def get(self, check_id: str) -> CheckResult:
        for c in self.checks:
            if c.id == check_id:
                return c
        raise KeyError(check_id)


def run_checks(record: EpisodeRecord, checks: Iterable[Check]) -> Scorecard:
    return Scorecard(episode_id=record.episode_id, checks=tuple(c.run(record) for c in checks))


def naive_weighted(card: Scorecard) -> float:
    """Strawman: the weighted mean of every check with no hard gate. A violation is
    just one more low score that volume on the other checks can buy back."""
    return card.quality


def from_report(
    report: VerifierReport,
    case_id: str,
    *,
    weight: float = 1.0,
    hard: bool = True,
    guards: tuple[str, ...] = (),
) -> CheckResult:
    """One run-level `VerifierReport` (ledger, policy, outcome) as a check for a case.

    This is how the original verifiers are split into weighted checks: the
    findings for the case (or run-wide, `*`) become the check's evidence."""
    mine = [f for f in report.findings if f.case_id in (case_id, "*")]
    return CheckResult(
        id=f"legacy.{report.name}",
        name=report.name,
        weight=weight,
        hard=hard,
        passed=not mine,
        score=0.0 if mine else 1.0,
        evidence=tuple(EvidenceRef(source="run", ref=f.kind, note=f.detail) for f in mine),
        explanation=report.anchored_to,
        guards=guards,
    )


# --- the first checks: outcome correctness and record integrity ------------------------


def _correct(r: EpisodeRecord) -> Verdict:
    o = r.outcome
    if o is None:
        return False, 0.0, (EvidenceRef(source="episode", ref=r.episode_id),), "no outcome"
    ok = o.cardholder_net_minor == r.case.truth.liability_minor
    ev = [EvidenceRef(source="case", ref="truth.liability_minor")]
    ev += [EvidenceRef(source="posting", ref=str(e.entry_id)) for e in r.journal]
    why = f"cardholder net {o.cardholder_net_minor} vs liability {r.case.truth.liability_minor}"
    return ok, 1.0 if ok else 0.0, ev, why


def _integrity(r: EpisodeRecord) -> Verdict:
    """The record is internally consistent and its trace chain is intact."""
    ev: list[EvidenceRef] = [at(s, "chain broken") for s in verify_events(r.trace)]
    problems: list[str] = ["chain"] if ev else []
    if not r.trace or r.trace[-1].hash != r.trace_head:
        problems.append("head")
        ev.append(EvidenceRef(source="episode", ref="trace_head"))
    posted = [e for e in r.trace if e.kind is EventKind.POSTING]
    journal = [e.model_dump(mode="json") for e in r.journal]
    if [p.data for p in posted] != journal:
        problems.append("journal_vs_trace")
        ev += [at(p.seq) for p in posted]
    o = r.outcome
    states = [e for e in r.trace if e.kind is EventKind.STATE]
    if o is not None:
        if o.liability_minor != r.case.truth.liability_minor:
            problems.append("liability")
        if o.correct != (o.cardholder_net_minor == o.liability_minor):
            problems.append("correct_flag")
        if states and states[-1].data.get("returned_minor") != o.cardholder_net_minor:
            problems.append("net_vs_trace")
            ev.append(at(states[-1].seq, "last recorded state"))
        resolved = [
            e
            for e in r.trace
            if e.kind is EventKind.TRANSITION and e.data["to_stage"] == "resolved"
        ]
        if len(resolved) != 1:
            problems.append("terminal_transition")
        if [s.value for s in o.path][1:] != [
            e.data["to_stage"] for e in r.trace if e.kind is EventKind.TRANSITION
        ]:
            problems.append("path_vs_trace")
    ok = not problems
    return ok, 1.0 if ok else 0.0, ev, "intact" if ok else "inconsistent: " + ", ".join(problems)


OUTCOME_CORRECT = FnCheck(
    "OUT-01",
    "cardholder net equals true liability",
    _correct,
    weight=3.0,
    guards=("X25", "X26"),
)
RECORD_INTEGRITY = FnCheck(
    "INV-05",
    "trace chain intact; journal, path and outcome agree with the trace",
    _integrity,
    weight=1.0,
    hard=True,
    guards=("X20", "X34"),
)

__all__ = [
    "OUTCOME_CORRECT",
    "RECORD_INTEGRITY",
    "BatchCheck",
    "BatchScorecard",
    "Check",
    "CheckResult",
    "EvidenceRef",
    "FnBatchCheck",
    "FnCheck",
    "Scorecard",
    "Verdict",
    "at",
    "from_report",
    "naive_weighted",
    "run_checks",
]
