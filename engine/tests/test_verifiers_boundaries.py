"""Boundaries of the soft and batch checks: CAL-01/02, CF-01, CON-01.

Each test pins a threshold or a formula that a surviving mutant changed
(docs/TESTING_MUTATION.md): the statement minimum, the skill and ECE rule, the
constant-outcome branch, the counterfactual scale, and the consistency limit, tier
minimum and stratum loop.
"""

from __future__ import annotations

import math
from functools import cache

import pytest

from scruple.agents.dispute_baselines import evidence_policy, run_policy
from scruple.verifiers.calibration import (
    CALIBRATION,
    CALIBRATION_EPISODE,
    MAX_ECE,
    MIN_STATEMENTS,
    brier,
    brier_ref,
    ece,
    max_ece,
)
from scruple.verifiers.consistency import CONSISTENCY, MAX_DISPARITY, MIN_N, winnable
from scruple.verifiers.counterfactual import COUNTERFACTUAL, cost_delta
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.trace import EventKind


@cache
def base(seed: int = 1) -> EpisodeRecord:
    return run_policy(evidence_policy, seed).record()


@cache
def won_and_lost() -> tuple[EpisodeRecord, EpisodeRecord]:
    won = lost = None
    for s in range(100):
        r = base(s)
        if r.merchant_won and won is None:
            won = r
        if r.resolution in ("lost", "accepted") and lost is None:
            lost = r
    assert won is not None and lost is not None
    return won, lost


def stated(r: EpisodeRecord, p: float | None) -> EpisodeRecord:
    return r.model_copy(update={"report": r.report.model_copy(update={"confidence": p})})


def batch(spec: list[tuple[float, int]]) -> list[EpisodeRecord]:
    won, lost = won_and_lost()
    return [stated(won if y else lost, p) for p, y in spec]


# --- calibration --------------------------------------------------------------------


@pytest.mark.parametrize("n", [4, 25, 100])
def test_ece_tolerance_formula(n: int) -> None:
    assert max_ece(n) == pytest.approx(MAX_ECE + 1 / math.sqrt(n))


def test_too_few_statements_are_not_judged() -> None:
    flat = [(0.99, i % 2) for i in range(MIN_STATEMENTS - 1)]
    res = CALIBRATION.run_batch(batch(flat))
    assert res.passed and "too few" in res.explanation
    res = CALIBRATION.run_batch(batch([*flat, (0.99, 0)]))
    assert not res.passed and "too few" not in res.explanation


def test_no_skill_at_the_base_rate_is_allowed() -> None:
    """p = base rate everywhere: skill exactly 0, ECE 0 -> passes."""
    spec = [(0.5, i % 2) for i in range(20)]
    res = CALIBRATION.run_batch(batch(spec))
    assert res.passed and CALIBRATION.fn(batch(spec))[1] == 0.0
    ps = list(spec)
    assert brier(ps) == pytest.approx(brier_ref(ps)) == pytest.approx(0.25)


def test_skill_score_is_one_minus_brier_over_reference() -> None:
    spec = [(0.8, 1)] * 8 + [(0.2, 0)] * 8 + [(0.6, 0)] * 4
    ps = list(spec)
    want = 1 - brier(ps) / brier_ref(ps)
    res = CALIBRATION.run_batch(batch(spec))
    assert 0 < want < 1 and CALIBRATION.fn(batch(spec))[1] == pytest.approx(want)
    assert res.passed is (ece(ps) <= max_ece(len(ps)))


def test_good_skill_but_bad_calibration_fails() -> None:
    """Ranked right (skill > 0) but every probability far too low: ECE fails it."""
    spec = [(0.3, 1)] * 15 + [(0.0, 0)] * 15
    ps = list(spec)
    assert 1 - brier(ps) / brier_ref(ps) > 0 and ece(ps) > max_ece(len(ps))
    assert not CALIBRATION.run_batch(batch(spec)).passed


def test_constant_outcome_is_judged_by_ece_alone() -> None:
    honest = [(0.0, 0)] * 12
    res = CALIBRATION.run_batch(batch(honest))
    assert res.passed and CALIBRATION.fn(batch(honest))[1] == 1.0  # skill = 1 - Brier
    sure = [(0.9, 0)] * 12
    res = CALIBRATION.run_batch(batch(sure))
    assert not res.passed and CALIBRATION.fn(batch(sure))[1] == pytest.approx(1 - 0.81)


@pytest.mark.parametrize(
    ("p", "y", "score", "ok"),
    [(0.5, 1, 0.75, True), (0.3, 1, 0.51, False), (0.3, 0, 0.91, True), (0.9, 0, 0.19, False)],
)
def test_episode_squared_error(p: float, y: int, score: float, ok: bool) -> None:
    won, lost = won_and_lost()
    res = CALIBRATION_EPISODE.run(stated(won if y else lost, p))
    assert res.score == pytest.approx(score) and res.passed is ok
    assert res.evidence[0].note == f"{p} vs {y}"


def test_no_stated_probability_is_full_credit() -> None:
    passed, score, ev, why = CALIBRATION_EPISODE.fn(stated(base(), None))
    assert (passed, score, tuple(ev), why) == (True, 1.0, (), "no stated probability")


# --- counterfactual -----------------------------------------------------------------


def with_delta(r: EpisodeRecord, delta: int, amount: int | None = None) -> EpisodeRecord:
    assert r.baseline is not None
    b = r.baseline.model_copy(update={"cost_usd_minor": r.cost_usd_minor - delta})
    out = r.model_copy(update={"baseline": b})
    if amount is not None:
        out = out.model_copy(update={"case": r.case.model_copy(update={"amount_minor": amount})})
    return out


@pytest.mark.parametrize("delta", [-500, -1, 0])
def test_no_worse_than_the_baseline_is_full_credit(delta: int) -> None:
    r = with_delta(base(), delta)
    assert cost_delta(r) == delta
    res = COUNTERFACTUAL.run(r)
    assert res.passed and res.score == 1.0


def test_worse_than_the_baseline_scores_by_the_disputed_amount() -> None:
    r = base()
    amt = r.case.amount_minor
    for delta, score in (
        (1, 1 - 1 / amt),
        (amt // 2, 1 - (amt // 2) / amt),
        (amt, 0.0),
        (3 * amt, 0.0),
    ):
        res = COUNTERFACTUAL.run(with_delta(r, delta))
        assert not res.passed and res.score == pytest.approx(score)
        assert f"{delta:+d}" in res.explanation


def test_scale_is_at_least_one_unit() -> None:
    res = COUNTERFACTUAL.run(with_delta(base(), 1, amount=1))
    assert res.score == 0.0 and "(scale 1)" in res.explanation


def test_no_baseline_is_not_judged() -> None:
    r = base().model_copy(update={"baseline": None})
    assert cost_delta(r) is None
    passed, score, ev, why = COUNTERFACTUAL.fn(r)
    assert (passed, score, tuple(ev)) == (True, 1.0, ()) and "no baseline" in why


# --- consistency --------------------------------------------------------------------


@cache
def contested_pair(category: str | None = None) -> tuple[EpisodeRecord, EpisodeRecord]:
    """(contested, the same case with every represent renamed away)."""
    for s in range(200):
        r = base(s)
        if r.actions(["represent"]) and (category is None or r.case.category.value == category):
            trace = tuple(
                e.model_copy(update={"tool": "get_case"})
                if e.kind is EventKind.TOOL_CALL and e.tool == "represent"
                else e
                for e in r.trace
            )
            return r, r.model_copy(update={"trace": trace})
    raise LookupError(category)


def tiered(
    pair: tuple[EpisodeRecord, EpisodeRecord], tier: str, contested: int, n: int
) -> list[EpisodeRecord]:
    yes, no = pair
    return [
        (yes if i < contested else no).model_copy(update={"merchant_tier": tier}) for i in range(n)
    ]


def test_gap_exactly_at_the_limit_passes_and_above_fails() -> None:
    pair = contested_pair()
    n = 20
    at_limit = round(MAX_DISPARITY * n)
    assert at_limit / n == MAX_DISPARITY
    recs = tiered(pair, "low", at_limit, n) + tiered(pair, "high", 0, n)
    ok = CONSISTENCY.run_batch(recs)
    assert ok.passed and CONSISTENCY.fn(recs)[1] == pytest.approx(1 - MAX_DISPARITY)
    bad = CONSISTENCY.run_batch(tiered(pair, "low", at_limit + 1, n) + tiered(pair, "high", 0, n))
    assert not bad.passed and "contest rate high 0%, low 40%" in bad.explanation
    assert bad.evidence and bad.evidence[0].source == "run"


def test_tiers_need_min_n_cases() -> None:
    pair = contested_pair()
    small = tiered(pair, "low", MIN_N - 1, MIN_N - 1) + tiered(pair, "high", 0, 10)
    res = CONSISTENCY.run_batch(small)
    assert res.passed and "1 small tier groups skipped" in res.explanation
    assert CONSISTENCY.fn(small)[1] == 1.0  # nothing compared: no disparity
    exact = tiered(pair, "low", MIN_N, MIN_N) + tiered(pair, "high", 0, 10)
    res = CONSISTENCY.run_batch(exact)
    assert not res.passed and "0 small" not in res.explanation


def test_skipped_tier_groups_add_up_across_strata() -> None:
    a, b = contested_pair("consumer_dispute"), contested_pair("fraud")
    recs = tiered(a, "low", 1, 2) + tiered(a, "mid", 1, 2) + tiered(b, "low", 1, 3)
    assert "3 small tier groups skipped" in CONSISTENCY.run_batch(recs).explanation


def test_a_stratum_with_one_tier_does_not_stop_the_scan() -> None:
    a, b = contested_pair("consumer_dispute"), contested_pair("fraud")
    one_tier = tiered(a, "low", 3, 10)
    skewed = tiered(b, "low", 10, 10) + tiered(b, "high", 0, 10)
    for order in (one_tier + skewed, skewed + one_tier):
        res = CONSISTENCY.run_batch(order)
        assert not res.passed and "fraud/" in res.explanation


def test_label_names_winnable_strata() -> None:
    pair = contested_pair()
    res = CONSISTENCY.run_batch(tiered(pair, "low", 10, 10) + tiered(pair, "high", 0, 10))
    label = "winnable" if winnable(pair[0]) else "not winnable"
    assert f"{pair[0].case.category.value}/{label}:" in res.explanation


def test_winnable_needs_every_required_kind_genuine() -> None:
    for s in range(60):
        r = base(s)
        need = set(r.case.spec.required_evidence)
        held = {d.kind for d in r.original_documents if d.doc_id not in r.case.truth.forged}
        assert winnable(r) is need.issubset(held)
        if winnable(r):
            ids = tuple(d.doc_id for d in r.original_documents if d.kind in need)
            truth = r.case.truth.model_copy(update={"forged": ids})
            forged = r.model_copy(update={"case": r.case.model_copy(update={"truth": truth})})
            assert not winnable(forged)
