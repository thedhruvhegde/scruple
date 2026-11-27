"""The scruple scorecard for dispute episodes: every check, in one place.

`score_record(record)` runs the per-episode checks; `score_batch(records)` adds the
checks that only make sense over many episodes. Both read `EpisodeRecord`s only.
"""

from __future__ import annotations

from collections.abc import Sequence

from scruple.verifiers.calibration import CALIBRATION, CALIBRATION_EPISODE
from scruple.verifiers.checks import (
    OUTCOME_CORRECT,
    RECORD_INTEGRITY,
    BatchCheck,
    BatchScorecard,
    Check,
    Scorecard,
    run_checks,
)
from scruple.verifiers.consistency import CONSISTENCY
from scruple.verifiers.counterfactual import COUNTERFACTUAL
from scruple.verifiers.deadlines_check import DEADLINE_CREDIT, DEADLINES
from scruple.verifiers.dispute_v3 import dispute_v3
from scruple.verifiers.escalation import ESCALATION_BASIS
from scruple.verifiers.evidence_check import EVIDENCE, EVIDENCE_FIT, EVIDENCE_RELEVANCE
from scruple.verifiers.faithfulness import FAITHFULNESS
from scruple.verifiers.ledger_invariants import LEDGER_INVARIANTS
from scruple.verifiers.memorisation import MEMORISATION
from scruple.verifiers.process import PROCESS, PROCESS_STEPS
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.scope import SCOPE
from scruple.verifiers.self_report import SELF_REPORT, TEXT_HYGIENE, WIN_RATE_CLAIM

DISPUTE_CHECKS_V2: tuple[Check, ...] = (
    RECORD_INTEGRITY,
    OUTCOME_CORRECT,
    LEDGER_INVARIANTS,
    COUNTERFACTUAL,
    PROCESS,
    PROCESS_STEPS,
    EVIDENCE,
    EVIDENCE_FIT,
    DEADLINES,
    DEADLINE_CREDIT,
    CALIBRATION_EPISODE,
    SELF_REPORT,
    TEXT_HYGIENE,
    SCOPE,
    ESCALATION_BASIS,
    FAITHFULNESS,
    EVIDENCE_RELEVANCE,
)
# round 6 (dispute@3): strict evidence provenance, required structured outcome claim,
# free-text ensemble, calibration that does not reward silence. `DISPUTE_CHECKS` is
# the current scorecard; `DISPUTE_CHECKS_V2` reproduces earlier grades.
DISPUTE_CHECKS_V3: tuple[Check, ...] = dispute_v3(DISPUTE_CHECKS_V2)
DISPUTE_CHECKS = DISPUTE_CHECKS_V3
BATCH_CHECKS: tuple[BatchCheck, ...] = (CONSISTENCY, CALIBRATION, WIN_RATE_CLAIM, MEMORISATION)


def score_record(record: EpisodeRecord, checks: Sequence[Check] = DISPUTE_CHECKS_V3) -> Scorecard:
    return run_checks(record, checks)


def score_batch(
    records: Sequence[EpisodeRecord],
    checks: Sequence[Check] = DISPUTE_CHECKS_V3,
    batch: Sequence[BatchCheck] = BATCH_CHECKS,
) -> BatchScorecard:
    return BatchScorecard(
        episodes=tuple(run_checks(r, checks) for r in records),
        checks=tuple(b.run_batch(records) for b in batch),
    )


__all__ = [
    "BATCH_CHECKS",
    "DISPUTE_CHECKS",
    "DISPUTE_CHECKS_V2",
    "DISPUTE_CHECKS_V3",
    "score_batch",
    "score_record",
]
