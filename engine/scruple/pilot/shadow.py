"""Shadow mode replayed over an imported history (pilot week 4, run on history).

Every stored case is sent as production's decision on the day it opened (the case as
it looked then: open, no outcome) with the action the history shows; the candidate is
scored on what was observable. Then the outcomes "arrive" (the resolved versions) and
costs are scored, each metric labelled observed or estimated. Same logic as
docs/pilot/demo/shadow_imported.py, as a function. Nothing is written anywhere.
"""

from __future__ import annotations

from datetime import timedelta

from scruple.backtest.candidate import Candidate
from scruple.backtest.history import History
from scruple.calibration import CalibrationProfile
from scruple.calibration.facts import contested, is_inquiry_level
from scruple.domain import Action
from scruple.gate.shadow_imported import (
    ImportedDecision,
    ImportedOutcome,
    ImportedShadowRecorder,
    ImportedShadowSummary,
)
from scruple.ingest.models import CaseState, LedgerMovement
from scruple.sim.models import Stage

DECISION_DELAY = timedelta(hours=2)


def replay_shadow(
    history: History, candidate: Candidate, profile: CalibrationProfile | None
) -> ImportedShadowSummary:
    moves: dict[str, list[LedgerMovement]] = {}
    for m in history.movements:
        moves.setdefault(m.case_external_id, []).append(m)
    rec = ImportedShadowRecorder(candidate, profile=profile, synthetic=history.synthetic)
    decisions = []
    for hc in history.cases:
        c = hc.case
        stage = Stage.INQUIRY if is_inquiry_level(c) else Stage.CHARGEBACK
        at_open = c.model_copy(
            update={"stage": stage, "state": CaseState.OPEN, "evidence_submitted": False}
        )
        decisions.append(
            ImportedDecision(
                case=at_open,
                action=Action.CONTEST if contested(c) else Action.ACCEPT,
                decided_at=c.opened_at + DECISION_DELAY,
                movements=tuple(
                    m for m in moves.get(c.external_id, []) if m.occurred_at <= c.opened_at
                ),
            )
        )
    rec.record(decisions)
    rec.observe(
        [
            ImportedOutcome(case=hc.case, movements=tuple(moves.get(hc.case.external_id, [])))
            for hc in history.cases
        ]
    )
    return rec.summary()


__all__ = ["DECISION_DELAY", "replay_shadow"]
