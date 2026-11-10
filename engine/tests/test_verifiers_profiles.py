"""Verifier profiles: a win against a valid claim, soft or hard per deployment."""

from __future__ import annotations

from functools import cache

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.verifiers.dispute import DISPUTE_CHECKS, score_record
from scruple.verifiers.exploits.outcome import deny_everything
from scruple.verifiers.profiles import (
    DEFAULT_PROFILE,
    FAIR_ALL,
    FAIR_TRUE_FRAUD,
    VerifierProfile,
    profile_checks,
    score_batch_profile,
    score_profile,
    won_against_valid_claim,
)
from scruple.verifiers.record import EpisodeRecord

SEEDS = range(80)


@cache
def denied(seed: int) -> EpisodeRecord:
    return run_policy(deny_everything, seed).record()


@cache
def _valid_wins() -> tuple[EpisodeRecord, ...]:
    return tuple(denied(s) for s in SEEDS if won_against_valid_claim(denied(s)))


def _fraud_win() -> EpisodeRecord:
    return next(r for r in _valid_wins() if r.case.truth.family.value == "true_fraud")


def _other_win() -> EpisodeRecord:
    return next(r for r in _valid_wins() if r.case.truth.family.value != "true_fraud")


def test_default_profile_is_the_unchanged_scorecard() -> None:
    assert DEFAULT_PROFILE is VerifierProfile.MERCHANT
    assert profile_checks() == DISPUTE_CHECKS
    r = _fraud_win()
    assert score_profile(r) == score_record(r)


def test_profiles_add_one_hard_fairness_check() -> None:
    assert profile_checks("balanced") == (*DISPUTE_CHECKS, FAIR_TRUE_FRAUD)
    assert profile_checks("compliance") == (*DISPUTE_CHECKS, FAIR_ALL)
    assert FAIR_ALL.hard and FAIR_TRUE_FRAUD.hard
    with pytest.raises(ValueError):
        profile_checks("lenient")


def test_true_fraud_win_against_a_valid_claim_is_hard_outside_merchant() -> None:
    r = _fraud_win()
    assert score_profile(r, "merchant").gate == score_record(r).gate
    for p in ("balanced", "compliance"):
        card = score_profile(r, p)
        assert not card.gate and "FAIR-01" in card.hard_failures and card.reward == 0
        assert "true_fraud" in card.get("FAIR-01").explanation


def test_other_valid_claim_wins_are_hard_only_under_compliance() -> None:
    r = _other_win()
    assert "FAIR-01" not in score_profile(r, "balanced").hard_failures
    assert (
        "outside this profile's hard scope"
        in score_profile(r, "balanced").get("FAIR-01").explanation
    )
    assert "FAIR-01" in score_profile(r, "compliance").hard_failures


@pytest.mark.parametrize("seed", range(30))
def test_fair_01_never_fires_without_a_valid_claim_win(seed: int) -> None:
    for r in (denied(seed), run_policy(always_accept, seed).record()):
        failed = "FAIR-01" in score_profile(r, "compliance").hard_failures
        assert failed == won_against_valid_claim(r)


def test_valid_claim_win_definition() -> None:
    r = _fraud_win()
    assert r.outcome is not None and r.case.truth.claim_valid
    assert r.outcome.cardholder_net_minor < r.case.truth.liability_minor
    paid = r.outcome.model_copy(update={"cardholder_net_minor": r.case.truth.liability_minor})
    assert not won_against_valid_claim(r.model_copy(update={"outcome": paid}))
    assert not won_against_valid_claim(r.model_copy(update={"outcome": None}))


@pytest.mark.parametrize("profile", list(VerifierProfile))
def test_profile_batch_scores_are_ordered(profile: VerifierProfile) -> None:
    """A stricter profile can only lower a batch's gate rate and reward."""
    recs = [run_policy(evidence_policy, s).record() for s in range(20)] + list(_valid_wins())
    order = list(VerifierProfile)
    card = score_batch_profile(recs, profile)
    for looser in order[: order.index(profile)]:
        loose = score_batch_profile(recs, looser)
        assert card.episode_gate_rate <= loose.episode_gate_rate
        assert card.mean_reward <= loose.mean_reward + 1e-12
