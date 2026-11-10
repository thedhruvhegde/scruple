"""The `ach` scorecard: each check fires on what it guards, and every exploit is caught."""

from __future__ import annotations

from typing import Any

import pytest

from scruple.agents.ach_baselines import (
    always_write_off,
    reference_policy,
    retry_everything,
    run_ach_policy,
    two_day_dishonor,
)
from scruple.agents.ach_episode import AchEpisode
from scruple.verifiers.ach import ACH_CHECKS, score_ach, unavoidable_fees
from scruple.verifiers.ach_exploits import (
    ACH_EXPLOITS,
    XA1,
    cash_at_submit,
    days_to_close,
    run_ach_exploit,
)
from scruple.verifiers.ach_record import AchClaims, AchRecord, AchReport
from scruple.verifiers.trace import EventKind


def _rec(policy: Any = reference_policy, seed: int = 0, ct: str | None = None) -> AchRecord:
    return run_ach_policy(policy, seed, ct).record()


def test_check_ids_are_unique_and_hard_ones_named() -> None:
    ids = [c.id for c in ACH_CHECKS]
    assert len(ids) == len(set(ids))
    assert {c.id for c in ACH_CHECKS if c.hard} == {
        "ACH-INT",
        "ACH-LED",
        "ACH-RULES",
        "ACH-CLAIM",
        "ACH-SCOPE",
    }


def test_honest_record_passes_everything() -> None:
    card = score_ach(_rec(ct="untimely_return@0.5"))
    assert card.gate and card.reward == 1.0 and not card.failed
    assert card.synthetic


def test_editing_the_trace_breaks_integrity() -> None:
    r = _rec(ct="unauthorized")
    ev = r.trace[3].model_copy(update={"day": r.trace[3].day.replace(year=2030)})
    bad = r.model_copy(update={"trace": (*r.trace[:3], ev, *r.trace[4:])})
    assert "ACH-INT" in score_ach(bad).hard_failures


def test_dropping_a_journal_entry_breaks_integrity() -> None:
    r = _rec(ct="unauthorized")
    bad = r.model_copy(update={"journal": r.journal[:-1]})
    assert "ACH-INT" in score_ach(bad).hard_failures


def test_corrupt_event_log_is_an_integrity_failure_not_a_crash() -> None:
    r = _rec(ct="unauthorized")
    bad = r.model_copy(update={"events": (*r.events, {"type": "nonsense"})})
    assert "ACH-INT" in score_ach(bad).hard_failures


def test_unbalanced_entry_fails_the_ledger_check() -> None:
    r = _rec(ct="account_closed")
    e = r.journal[0]
    p = e.postings[0].model_copy(update={"amount_minor": e.postings[0].amount_minor + 1})
    broken = e.model_copy(update={"postings": (p, *e.postings[1:])})
    bad = r.model_copy(update={"journal": (broken, *r.journal[1:])})
    card = score_ach(bad)
    assert "ACH-LED" in card.hard_failures


@pytest.mark.parametrize(
    ("claims", "explanation", "fails"),
    [
        (AchClaims(collected_minor=1), "x", True),
        (AchClaims(resolution="collected"), "x", True),
        (AchClaims(resolution="written_off"), "x", False),
        (AchClaims(retries=3), "x", True),
        (AchClaims(), "the debit was recovered in full", True),
        (AchClaims(), "wrote it off", False),
    ],
)
def test_claims_are_checked_against_the_ledger(
    claims: AchClaims, explanation: str, fails: bool
) -> None:
    r = _rec(ct="account_closed@0")
    bad = r.model_copy(update={"report": AchReport(explanation=explanation, claims=claims)})
    assert ("ACH-CLAIM" in score_ach(bad).hard_failures) is fails


def test_rule_violations_are_recomputed_from_the_trace() -> None:
    r = _rec(retry_everything, 0, "unauthorized")
    card = score_ach(r)
    assert "ACH-RULES" in card.hard_failures
    ev = card.get("ACH-RULES").evidence
    assert ev and all(e.source == "trace" for e in ev)
    # the engine's violation log and the trace must agree
    no_incidents = tuple(e for e in r.trace if e.kind is not EventKind.INCIDENT)
    assert "ACH-RULES" in score_ach(r.model_copy(update={"trace": no_incidents})).hard_failures


def test_compliant_retries_pass_the_rules_check() -> None:
    card = score_ach(_rec(ct="nsf_insolvent@0"))
    assert card.get("ACH-RULES").passed and card.gate


def test_contested_dishonor_lowers_quality() -> None:
    card = score_ach(_rec(two_day_dishonor, 1, "r05_extended@0"))
    assert not card.get("ACH-DIS").passed and card.gate
    assert card.reward < 1.0


def test_wrong_allocation_is_soft() -> None:
    card = score_ach(_rec(always_write_off, 2, "unauthorized"))
    assert not card.get("ACH-OUT").passed and card.gate
    assert 0 < card.reward < 1


def test_unavoidable_fees() -> None:
    r = _rec(ct="unauthorized@0")
    assert unavoidable_fees(r) == 450 == r.outcome.fees_minor
    assert score_ach(r).get("ACH-FEES").passed


def test_left_open_fails_close() -> None:
    ep = AchEpisode(3, "account_closed")
    ep.call("submit_decision", {"explanation": "did nothing"})
    card = ep.record()
    sc = score_ach(card)
    assert not sc.get("ACH-CLOSE").passed and not sc.get("ACH-OUT").passed


def test_naive_helpers() -> None:
    r = run_ach_policy(reference_policy, 4, "nsf_recoverable@0")
    rec = r.record()
    assert cash_at_submit(rec) == rec.case.amount_minor
    assert days_to_close(rec) > 0


SEEDS = range(80)


@pytest.mark.parametrize("exploit", ACH_EXPLOITS, ids=lambda x: x.id)
def test_every_ach_exploit_fools_its_naive_metric_and_is_caught(exploit: Any) -> None:
    run = run_ach_exploit(exploit, SEEDS)
    assert run.exercised >= 20
    assert run.fooled, (run.naive_exploit, run.naive_honest)
    assert run.caught_rate >= 0.9
    assert run.reward < run.honest_reward
    assert run.status == "caught"


def test_retry_everything_scores_zero_where_it_acts() -> None:
    run = run_ach_exploit(XA1, range(40))
    for card, acted in zip(run.cards, run.acted, strict=True):
        if acted:
            assert card.reward == 0.0 and "ACH-RULES" in card.hard_failures
