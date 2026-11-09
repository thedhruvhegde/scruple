"""Run the gate on one candidate rule, as a CI job does, and build its scorecard.

The run goes through `AgentGate` like every other caller, so a CI evaluation is
audited exactly as an API evaluation is. With the default in-memory database and
fixed clock the run is reproducible: the same candidate and config give the same
scorecard bytes, so a reviewer can rerun a pull request's check and diff it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from scruple.ci.config import CandidateFile, CIConfig
from scruple.ci.scorecard import Scorecard, build_scorecard
from scruple.clock import Clock, SteppingClock, SystemClock
from scruple.gate.models import DecisionRecord
from scruple.service import AgentGate

CI_ACTOR = "scruple-ci"
FIXED_START = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class CIRun:
    gate: AgentGate
    records: tuple[DecisionRecord, ...]
    scorecard: Scorecard


def ci_clock(db: str, fixed: bool | None = None) -> Clock:
    """Fixed clock for throwaway in-memory runs (reproducible), wall clock otherwise."""
    use_fixed = db == ":memory:" if fixed is None else fixed
    return SteppingClock(FIXED_START) if use_fixed else SystemClock()


def run_candidate(cfg: CIConfig, candidate: CandidateFile, gate: AgentGate | None = None) -> CIRun:
    if gate is None:
        gate = AgentGate.open(cfg.ci.db, ci_clock(cfg.ci.db), cfg.gate)
    gate.bootstrap_baseline(CI_ACTOR)
    rv = gate.propose(candidate.rule_id, candidate.spec, candidate.owner, candidate.provenance)
    records = tuple(gate.evaluate(rv.rule_id, rv.version, CI_ACTOR))
    card = build_scorecard(records, gate.config, gate.audit.head(), cfg.ci.fail_on_hold)
    return CIRun(gate=gate, records=records, scorecard=card)
