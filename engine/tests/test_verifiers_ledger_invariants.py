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


# --- the honest policy's INV-01 alarms (benchmark v1: 19 of 10,000) -----------------------
# Seeds 0-5999 where the evidence policy failed INV-01 before the engine fix. All had a
# refund racing the dispute; three engine faults put the cardholder over-credited at close:
#   partial dispute: the genuine refund record (the disputed amount) failed the amount
#     check against the capture, so neither the policy nor the issuer used it;
#   split shipment: a moot response won only the parcels proven delivered (none);
#   partial dispute, refund settling after the chargeback debit: the ledger only
#     checked headroom against the capture, so the dispute was paid twice.
RACE_PARTIAL = (647, 2046, 2836, 3392, 4317, 4391, 4650)
RACE_SPLIT = (950, 1277, 2725)
RACE_AFTER_DEBIT = (2127, 5689)


@pytest.mark.parametrize("seed", RACE_PARTIAL + RACE_SPLIT + RACE_AFTER_DEBIT)
def test_refund_races_no_longer_fail_the_honest_policy(seed: int) -> None:
    rec = run_policy(evidence_policy, seed).record()
    assert rec.case.refund_in_flight is not None  # still the case that used to fail
    res = LEDGER_INVARIANTS.run(rec)
    assert res.passed, res.explanation
    assert rec.outcome is not None
    assert rec.outcome.cardholder_net_minor <= rec.case.amount_minor


@pytest.mark.parametrize("seed", RACE_PARTIAL + RACE_SPLIT)
def test_settled_refund_record_makes_the_dispute_moot(seed: int) -> None:
    rec = run_policy(evidence_policy, seed).record()
    cited = {i for e in rec.actions(["represent"]) for i in (e.args or {}).get("evidence_ids", [])}
    kinds = {d.kind.value for d in rec.documents if d.doc_id in cited}
    assert kinds == {"refund_record"}
    assert rec.resolution == "won"
    assert rec.outcome is not None and rec.outcome.cardholder_net_minor == rec.case.amount_minor


@pytest.mark.parametrize("seed", RACE_AFTER_DEBIT)
def test_race_refund_after_the_debit_is_refused(seed: int) -> None:
    rec = run_policy(evidence_policy, seed).record()
    assert rec.case.amount_minor < rec.case.captured_minor  # headroom was left
    assert any(i.kind == "refund_blocked" for i in rec.incidents)
    refunds = [e for e in rec.trace if e.kind is EventKind.POSTING and e.data["kind"] == "refund"]
    assert refunds == []


def test_partial_refund_record_is_genuine_but_not_above_the_capture() -> None:
    from datetime import date

    from scruple.sim.evidence import EvidenceKind, document_problems, make_document

    day = date(2026, 3, 1)

    def rec(amount: str, kind: EvidenceKind = EvidenceKind.REFUND_RECORD) -> tuple[str, ...]:
        doc = make_document("c", kind, day, {"amount_minor": amount, "currency": "USD"}, 0)
        return document_problems(doc, 1_000, "USD", day)

    assert rec("1") == rec("400") == rec("1000") == ()
    for bad in ("0", "1001", "-5", "4x"):
        assert rec(bad) == ("amount_mismatch",)
    assert rec("400", EvidenceKind.RECEIPT) == ("amount_mismatch",)  # other kinds: the capture


def test_agent_double_credit_is_still_caught() -> None:
    """The fix narrows only environment-caused over-credits; an agent that refunds on
    top of a chargeback (X13) still fails INV-01."""
    from scruple.verifiers.exploits import EXPLOIT_BY_ID, run_exploit

    run = run_exploit(EXPLOIT_BY_ID["X13"], range(40))
    assert run.status == "caught" and run.fired["INV-01"] == run.exercised > 0
