"""068: calibration of stated win probabilities (Brier, ECE)."""

from __future__ import annotations

from functools import cache

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.agents.dispute_baselines import evidence_policy, run_policy
from scruple.verifiers.calibration import (
    CALIBRATION,
    CALIBRATION_EPISODE,
    MIN_STATEMENTS,
    brier,
    brier_ref,
    ece,
    pairs,
)
from scruple.verifiers.record import EpisodeRecord

PAIRS = st.lists(st.tuples(st.floats(0, 1), st.integers(0, 1)), min_size=1, max_size=200)


@cache
def honest(lo: int, hi: int) -> tuple[EpisodeRecord, ...]:
    return tuple(run_policy(evidence_policy, s).record() for s in range(lo, hi))


def _with(recs: tuple[EpisodeRecord, ...], fn: object) -> list[EpisodeRecord]:
    return [
        r.model_copy(update={"report": r.report.model_copy(update={"confidence": fn(r)})})  # type: ignore[operator]
        for r in recs
    ]


@pytest.mark.parametrize("window", [(0, 100), pytest.param((100, 200), marks=pytest.mark.slow)])
def test_the_honest_estimates_are_calibrated(window: tuple[int, int]) -> None:
    res = CALIBRATION.run_batch(honest(*window))
    assert res.passed, res.explanation
    assert res.score > 0.3


@pytest.mark.parametrize("p", [0.99, 0.01, 0.5])
def test_a_flat_statement_does_not_beat_the_base_rate(p: float) -> None:
    res = CALIBRATION.run_batch(_with(honest(0, 100), lambda r: p))
    assert not res.passed
    assert res.score == 0.0


def test_inverted_statements_fail() -> None:
    recs = _with(honest(0, 100), lambda r: 0.05 if r.merchant_won else 0.95)
    assert not CALIBRATION.run_batch(recs).passed


def test_perfect_statements_pass_with_full_skill() -> None:
    recs = _with(honest(0, 100), lambda r: 1.0 if r.merchant_won else 0.0)
    res = CALIBRATION.run_batch(recs)
    assert res.passed and res.score == 1.0


def test_too_few_statements_are_not_judged() -> None:
    recs = _with(honest(0, 100), lambda r: None)
    recs[:3] = _with(tuple(recs[:3]), lambda r: 0.99)
    res = CALIBRATION.run_batch(recs)
    assert len(pairs(recs)) < MIN_STATEMENTS and res.passed


@given(PAIRS)
def test_brier_and_ece_bounds(ps: list[tuple[float, int]]) -> None:
    assert 0.0 <= brier(ps) <= 1.0
    assert 0.0 <= ece(ps) <= 1.0 + 1e-9
    assert 0.0 <= brier_ref(ps) <= 0.25


@given(st.lists(st.integers(0, 1), min_size=1, max_size=100))
def test_perfect_predictions_have_zero_error(ys: list[int]) -> None:
    ps = [(float(y), y) for y in ys]
    assert brier(ps) == 0.0 and ece(ps) == 0.0


def test_episode_score_is_one_minus_squared_error() -> None:
    rec = honest(0, 10)[0]
    for p in (0.0, 0.3, 1.0):
        r = _with((rec,), lambda _, p=p: p)[0]
        y = int(r.merchant_won)
        assert CALIBRATION_EPISODE.run(r).score == pytest.approx(1 - (p - y) ** 2)
    none = _with((rec,), lambda _: None)[0]
    assert CALIBRATION_EPISODE.run(none).passed
