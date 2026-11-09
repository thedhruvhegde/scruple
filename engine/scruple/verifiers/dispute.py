"""The scruple scorecard for dispute episodes: every check, in one place.

`score_record(record)` runs the per-episode checks; `score_batch(records)` adds the
checks that only make sense over many episodes. Both read `EpisodeRecord`s only.
"""

from __future__ import annotations

from collections.abc import Sequence

from scruple.verifiers.checks import (
    OUTCOME_CORRECT,
    RECORD_INTEGRITY,
    BatchCheck,
    BatchScorecard,
    Check,
    Scorecard,
    run_checks,
)
from scruple.verifiers.counterfactual import COUNTERFACTUAL
from scruple.verifiers.ledger_invariants import LEDGER_INVARIANTS
from scruple.verifiers.record import EpisodeRecord

DISPUTE_CHECKS: tuple[Check, ...] = (
    RECORD_INTEGRITY,
    OUTCOME_CORRECT,
    LEDGER_INVARIANTS,
    COUNTERFACTUAL,
)
BATCH_CHECKS: tuple[BatchCheck, ...] = ()


def score_record(record: EpisodeRecord, checks: Sequence[Check] = DISPUTE_CHECKS) -> Scorecard:
    return run_checks(record, checks)


def score_batch(
    records: Sequence[EpisodeRecord],
    checks: Sequence[Check] = DISPUTE_CHECKS,
    batch: Sequence[BatchCheck] = BATCH_CHECKS,
) -> BatchScorecard:
    return BatchScorecard(
        episodes=tuple(run_checks(r, checks) for r in records),
        checks=tuple(b.run_batch(records) for b in batch),
    )


__all__ = ["BATCH_CHECKS", "DISPUTE_CHECKS", "score_batch", "score_record"]
