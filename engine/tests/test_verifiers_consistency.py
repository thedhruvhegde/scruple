"""067: similar cases treated similarly across merchant segments."""

from __future__ import annotations

from functools import cache

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.verifiers.consistency import CONSISTENCY, MIN_N, contested, rates, stratum
from scruple.verifiers.record import EpisodeRecord


@cache
def honest(lo: int, hi: int) -> tuple[EpisodeRecord, ...]:
    return tuple(run_policy(evidence_policy, s).record() for s in range(lo, hi))


@pytest.mark.parametrize("window", [(0, 200), pytest.param((200, 400), marks=pytest.mark.slow)])
def test_the_honest_policy_is_consistent(window: tuple[int, int]) -> None:
    res = CONSISTENCY.run_batch(honest(*window))
    assert res.passed, res.explanation
    assert res.hard and res.id == "CON-01"


def test_rates_count_every_record_once() -> None:
    recs = honest(0, 200)
    table = rates(recs)
    assert sum(n for tiers in table.values() for _, n in tiers.values()) == len(recs)
    for r in recs[:20]:
        c, _ = table[stratum(r)][r.merchant_tier or "unknown"]
        assert c >= int(contested(r))


def test_conceding_for_one_segment_is_flagged() -> None:
    recs = honest(0, 200)
    # medium-tier merchants get a policy that concedes everything; others stay honest
    biased = [
        run_policy(always_accept, r.seed).record() if r.merchant_tier == "medium" else r
        for r in recs
    ]
    res = CONSISTENCY.run_batch(biased)
    assert not res.passed and res.evidence
    assert "medium 0%" in res.explanation


def test_small_groups_are_skipped_not_judged() -> None:
    recs = honest(0, MIN_N)  # too few per tier for any comparison
    res = CONSISTENCY.run_batch(recs)
    assert res.passed and "skipped" in res.explanation
