"""062: ledger invariants checked after every action, from the trace."""

from __future__ import annotations

from typing import Any

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.env import FAMILIES
from scruple.verifiers.dispute import score_record
from scruple.verifiers.ledger_invariants import LEDGER_INVARIANTS
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.trace import EventKind

POLICIES = {"evidence": evidence_policy, "always_accept": always_accept}


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_honest_policies_hold_every_invariant(policy: str, seed: int, family: str) -> None:
    rec = run_policy(POLICIES[policy], seed, family).record()
    res = LEDGER_INVARIANTS.run(rec)
    assert res.passed, res.explanation
    assert res.hard and res.id == "INV-01"


def _edit_posting(rec: EpisodeRecord, fn: Any) -> EpisodeRecord:
    trace = list(rec.trace)
    for i, e in enumerate(trace):
        if e.kind is EventKind.POSTING and e.data["kind"] == "chargeback_debit":
            trace[i] = e.model_copy(update={"data": fn(dict(e.data))})
            break
    return rec.model_copy(update={"trace": tuple(trace)})


def _chargeback_record(seed: int) -> EpisodeRecord:
    for s in range(seed, seed + 50):
        rec = run_policy(always_accept, s).record()
        if any(
            e.kind is EventKind.POSTING and e.data["kind"] == "chargeback_debit" for e in rec.trace
        ):
            return rec
    raise AssertionError  # pragma: no cover


@pytest.mark.parametrize("seed", range(0, 40, 5))
def test_unbalanced_entry_is_caught(seed: int) -> None:
    rec = _chargeback_record(seed)

    def skew(d: dict[str, Any]) -> dict[str, Any]:
        posts = [dict(p) for p in d["postings"]]
        posts[0]["amount_minor"] += 1
        return {**d, "postings": posts}

    res = LEDGER_INVARIANTS.run(_edit_posting(rec, skew))
    assert not res.passed and "INV-01 unbalanced entry" in res.explanation
    assert any(ref.source == "trace" for ref in res.evidence)


@pytest.mark.parametrize("seed", range(0, 40, 5))
def test_vanishing_money_breaks_conservation(seed: int) -> None:
    rec = _chargeback_record(seed)
    keep = tuple(
        e
        for e in rec.trace
        if not (e.kind is EventKind.POSTING and e.data["kind"] == "chargeback_debit")
    )
    res = LEDGER_INVARIANTS.run(rec.model_copy(update={"trace": keep}))
    if rec.case.currency == rec.case.settlement_currency:
        assert not res.passed and "conservation" in res.explanation


def test_refund_on_top_of_a_chargeback_is_caught_mid_episode() -> None:
    for seed in range(400):
        ep = run_policy(always_accept, seed, "service_failure")
        case = ep.session.case
        if (
            case.opening_stage.value == "chargeback"
            and 2 * case.amount_minor <= case.captured_minor
        ):
            break
    else:  # pragma: no cover
        raise AssertionError
    ep = DisputeEpisode(seed, "service_failure")
    ep.call("refund", {"amount_minor": case.amount_minor})
    ep.call("accept")
    score = ep.submit(run_policy(always_accept, seed).record().report)
    card = score.scorecard
    assert card is not None and not card.gate
    res = card.get("INV-01")
    assert "agent refund on top of the dispute" in res.explanation
    assert "over-credited at close" in res.explanation
    assert score_record(ep.record()).reward == 0.0
