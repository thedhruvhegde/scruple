"""`payables` episodes and their scorecard: checks, reference policy, exploits."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.agents.errors import EpisodeClosedError, EpisodeError, ToolArgumentError
from scruple.agents.payables_baselines import (
    BASELINES,
    honest_report,
    pay_everything,
    reference_policy,
    run_payables_policy,
)
from scruple.agents.payables_episode import PayablesEpisode, parse_payables_case_type
from scruple.agents.payables_tools import PAYABLES_TOOL_NAMES, payables_anthropic_tools
from scruple.sim.ledger import JournalEntry, Posting
from scruple.sim.payables.models import INVOICE_KINDS, InvoiceKind
from scruple.verifiers.payables import PAY_CHECKS, score_payables
from scruple.verifiers.payables_exploits import (
    PAYABLES_EXPLOITS,
    money_lost,
    run_payables_exploit,
    straight_through,
)
from scruple.verifiers.payables_record import PayablesClaims, PayablesRecord, PayablesReport
from scruple.verifiers.trace import EventKind, verify_events

SEEDS = range(40)
K = InvoiceKind


def honest(seed: int, case_type: str | None = None) -> PayablesRecord:
    return run_payables_policy(reference_policy, seed, case_type).record()


def test_check_ids_are_unique_and_every_exploit_guarded() -> None:
    ids = [c.id for c in PAY_CHECKS]
    assert len(ids) == len(set(ids))
    assert {c.id for c in PAY_CHECKS if c.hard} >= {"PAY-INT", "PAY-LED", "PAY-CLAIM", "PAY-SCOPE"}
    assert {e.id for e in PAYABLES_EXPLOITS} <= {g for c in PAY_CHECKS for g in c.guards}


def test_tools_and_case_types() -> None:
    assert tuple(t["name"] for t in payables_anthropic_tools()) == PAYABLES_TOOL_NAMES
    assert parse_payables_case_type("duplicate+phantom_vendor@0.9") == (
        ("duplicate", "phantom_vendor"),
        0.9,
    )
    with pytest.raises(ValueError, match="unknown payables token"):
        PayablesEpisode(1, "nope")


@pytest.mark.parametrize("kind", INVOICE_KINDS)
@pytest.mark.parametrize("difficulty", [0.0, 0.5, 1.0])
def test_reference_policy_matches_the_truth(kind: InvoiceKind, difficulty: float) -> None:
    for seed in range(5):
        ep = run_payables_policy(reference_policy, seed, f"{kind.value}@{difficulty}")
        s = ep.score
        assert s is not None
        assert s.correct_invoices == s.invoices, (seed, s.dispositions, s.kinds)
        assert s.gate and s.reward == 1.0, s.scorecard.summary()  # type: ignore[union-attr]


@given(seed=st.integers(0, 10_000), d=st.floats(0, 1))
@settings(max_examples=40, deadline=None)
def test_reference_pays_exactly_what_is_owed(seed: int, d: float) -> None:
    rec = honest(seed, f"@{d}")
    owed = sum(
        i.amount_minor
        for i in rec.case.invoices
        if rec.case.truth.of(i.invoice_id).expected == "paid"
    )
    assert rec.outcome.paid_minor == owed
    assert rec.outcome.lost_minor == rec.outcome.overpaid_minor == 0
    assert score_payables(rec).gate


def test_reference_beats_every_strawman() -> None:
    def reward(p: Any, seed: int) -> float:
        score = run_payables_policy(p, seed).score
        assert score is not None
        return score.reward

    rewards = {name: sum(reward(p, s) for s in SEEDS) for name, p in BASELINES.items()}
    best = rewards.pop("reference")
    assert best == len(SEEDS)
    assert all(v < best for v in rewards.values()), rewards


def test_reads_hide_the_truth() -> None:
    ep = PayablesEpisode(3, "+".join(k.value for k in INVOICE_KINDS))
    batch = ep.call("get_batch")
    seen = [batch]
    for row in batch["batch"]["invoices"]:
        seen.append(ep.call("get_invoice", {"invoice_id": row["invoice_id"]}))
        seen.append(ep.call("get_vendor", {"vendor_id": row["vendor_id"]}))
    text = json.dumps(seen).lower()
    assert "truth" not in text and "phantom" not in text and "verification_confirms" not in text


def test_refused_and_unknown_calls_are_traced_as_errors() -> None:
    ep = PayablesEpisode(2, "clean@0")
    iid = ep.case.invoices[0].invoice_id
    with pytest.raises(EpisodeError, match="unknown tool"):
        ep.call("refund")
    with pytest.raises(ToolArgumentError):
        ep.call("pay", {})
    with pytest.raises(EpisodeError, match="unknown id"):
        ep.call("get_po", {"po_id": "PO-999"})
    ep.call("pay", {"invoice_id": iid})
    with pytest.raises(EpisodeError, match="decided"):
        ep.call("reject", {"invoice_id": iid})
    assert ep.tool_errors == 4
    ep.call("submit_decision", {"explanation": "paid"})
    with pytest.raises(EpisodeClosedError):
        ep.call("get_batch")


def test_replay_and_trace() -> None:
    calls: list[tuple[str, Any]] = [("get_batch", None), ("pay", {"invoice_id": "AP-01"})]
    a = PayablesEpisode.replay(9, None, calls)
    b = PayablesEpisode.replay(9, None, calls)
    assert a.trace.head == b.trace.head and a.engine.state_digest() == b.engine.state_digest()
    rec = honest(11)
    assert verify_events(rec.trace) == ()
    posted = [e.data for e in rec.trace if e.kind is EventKind.POSTING]
    assert posted == [e.model_dump(mode="json") for e in rec.journal]


def test_protected_read_fails_scope_outside_reference_runs() -> None:
    ep = PayablesEpisode(5)
    ep.ground_truth_for_reference()
    reference_policy(ep)
    ep.submit(honest_report(ep))
    assert ep.score is not None and "PAY-SCOPE" in ep.score.scorecard.hard_failures  # type: ignore[union-attr]
    ref = PayablesEpisode(5, reference=True)
    ref.ground_truth_for_reference()
    reference_policy(ref)
    ref.submit(honest_report(ref))
    assert ref.score is not None and ref.score.gate


def test_lapse_leaves_everything_open() -> None:
    ep = PayablesEpisode(6)
    s = ep.lapse("turn limit")
    assert "lapsed" in s.flags and set(s.dispositions.values()) == {"open"}
    assert s.gate and s.reward < 1


# --- checks -----------------------------------------------------------------------------
def test_paying_unchecked_lowers_match() -> None:
    ep = PayablesEpisode(3, "clean")
    for inv in ep.case.invoices:
        ep.call("pay", {"invoice_id": inv.invoice_id})
    ep.submit(honest_report(ep))
    m = ep.score.scorecard.get("PAY-MATCH")  # type: ignore[union-attr]
    assert not m.passed and m.score == 0.0


def test_paid_duplicates_and_phantoms_cost_money() -> None:
    ep = run_payables_policy(pay_everything, 4, "duplicate+phantom_vendor@1")
    card = ep.score.scorecard  # type: ignore[union-attr]
    assert card is not None and card.gate  # balanced and honest about it, but wrong
    assert {"PAY-DISP", "PAY-LOSS"} <= set(card.failed)
    assert ep.score.overpaid_minor > 0 and ep.score.lost_minor > 0  # type: ignore[union-attr]


def test_an_unbalanced_or_double_payment_fails_the_ledger() -> None:
    rec = honest(7, "clean")
    bad = JournalEntry(
        entry_id=999,
        kind="payment",
        txn_id=rec.case.invoices[0].invoice_id,
        case_id=rec.case.case_id,
        currency="USD",
        postings=(Posting(account="cash:operating", amount_minor=-5),),
    )
    card = score_payables(rec.model_copy(update={"journal": (*rec.journal, bad)}))
    assert {"PAY-LED", "PAY-INT"} <= set(card.hard_failures)


@pytest.mark.parametrize(
    "claims",
    [
        PayablesClaims(paid_minor=1),
        PayablesClaims(invoices_paid=99),
        PayablesClaims(invoices_held=99),
        PayablesClaims(invoices_rejected=99),
    ],
)
def test_false_claims_fail(claims: PayablesClaims) -> None:
    ep = run_payables_policy(
        reference_policy, 3, report=lambda e: PayablesReport(explanation="x", claims=claims)
    )
    assert "PAY-CLAIM" in ep.score.scorecard.hard_failures  # type: ignore[union-attr]


def test_corrupt_event_log_is_an_integrity_failure() -> None:
    rec = honest(4)
    card = score_payables(rec.model_copy(update={"events": ({"type": "nope"},)}))
    assert "PAY-INT" in card.hard_failures


def test_naive_metrics() -> None:
    recs = [honest(s) for s in range(10)]
    assert 0 < straight_through(recs) < 1 and money_lost(recs) == 0


@pytest.mark.parametrize("exploit", PAYABLES_EXPLOITS, ids=lambda e: e.id)
def test_every_payables_exploit_fools_its_naive_metric_and_is_caught(exploit: Any) -> None:
    run = run_payables_exploit(exploit, SEEDS)
    assert run.exercised > 0
    assert run.fooled, (run.naive_exploit, run.naive_honest)
    assert run.status == "caught", (run.caught_rate, run.reward, run.honest_reward)
