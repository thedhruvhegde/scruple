from __future__ import annotations

from collections import Counter

import pytest

from scruple.domain import FIELD_TYPES, Action
from scruple.hashing import canonical_json
from scruple.sim import Policy, Scenario, Simulator, generate_cases, oracle, outcome_for
from scruple.sim.engine import Outcome
from scruple.sim.ledger import Ledger, UnbalancedEntryError
from scruple.sim.library import baseline_policy
from scruple.sim.models import ARBITRATION_FEE_USD, REPRESENTMENT_LOSS_FEE_USD, Resolution


def _bytes(seed: int, n: int = 500) -> str:
    return canonical_json([c.model_dump(mode="json") for c in generate_cases(seed, n)])


def test_same_seed_gives_byte_identical_cases() -> None:
    assert _bytes(7) == _bytes(7)


def test_different_seed_gives_different_cases() -> None:
    assert _bytes(7) != _bytes(8)


def test_same_seed_gives_byte_identical_run() -> None:
    a = Simulator(generate_cases(11, 800)).run(baseline_policy())
    b = Simulator(generate_cases(11, 800)).run(baseline_policy())
    assert canonical_json(a.model_dump(mode="json")) == canonical_json(b.model_dump(mode="json"))
    assert a.digest() == b.digest()


def test_generates_requested_count_and_all_scenarios() -> None:
    cases = generate_cases(3, 3000)
    assert len(cases) == 3000
    seen = Counter(c.scenario for c in cases)
    assert set(seen) == set(Scenario)
    assert len({c.case_id for c in cases}) == len(cases)


def test_duplicates_follow_their_original() -> None:
    cases = generate_cases(5, 2000)
    for i, c in enumerate(cases):
        if c.scenario is Scenario.DUPLICATE_DISPUTE:
            assert cases[i - 1].txn_id == c.txn_id
            assert c.prior_dispute_on_txn and not c.claim_valid


def test_view_exposes_exactly_the_rule_fields_and_no_ground_truth() -> None:
    view = generate_cases(1, 1)[0].view()
    assert set(view) == set(FIELD_TYPES)
    assert "claim_valid" not in view and "scenario" not in view


def test_snapshot_and_restore_mid_run_is_equivalent() -> None:
    cases = generate_cases(21, 400)
    policy = baseline_policy()
    full = Simulator(cases).run(policy)

    sim = Simulator(cases)
    for _ in range(150):
        sim.step(policy)
    snap = sim.snapshot()
    for _ in range(100):  # wander off with a different policy
        sim.step(Policy(rules=(), default_action=Action.CONTEST))
    sim.restore(snap)
    while not sim.done:
        sim.step(policy)
    assert sim.result().digest() == full.digest()


def test_ledger_rejects_unbalanced_entries() -> None:
    with pytest.raises(UnbalancedEntryError):
        Ledger().post("x", "t", None, "USD", [("a", 10), ("b", -9)])


def test_every_journal_entry_balances() -> None:
    run = Simulator(generate_cases(9, 1000)).run(baseline_policy())
    for e in run.journal:
        assert sum(p.amount_minor for p in e.postings) == 0


def test_lifecycle_contest_late_evidence_loses_with_fee() -> None:
    c = generate_cases(1, 1)[0].model_copy(
        update={"evidence_ready_day": 40, "response_deadline_day": 30}
    )
    o = outcome_for(c, Action.CONTEST)
    assert o.resolution is Resolution.LOST and o.fee_usd_minor == REPRESENTMENT_LOSS_FEE_USD


def test_lifecycle_contest_valid_claim_goes_to_arbitration() -> None:
    c = generate_cases(1, 1)[0].model_copy(
        update={"claim_valid": True, "refunded_minor": 0, "evidence_ready_day": 1}
    )
    o = outcome_for(c, Action.CONTEST)
    assert o.resolution is Resolution.LOST and o.fee_usd_minor == ARBITRATION_FEE_USD
    assert "arbitration" in [s.value for s in o.stages]


def test_lifecycle_contest_invalid_claim_wins() -> None:
    c = generate_cases(1, 1)[0].model_copy(update={"claim_valid": False, "evidence_ready_day": 1})
    o: Outcome = outcome_for(c, Action.CONTEST)
    assert o.resolution is Resolution.WON and o.debit_txn_minor == 0


def test_partial_refund_contest_is_partial_win() -> None:
    c = next(c for c in generate_cases(2, 2000) if c.scenario is Scenario.PARTIAL_REFUND)
    o = outcome_for(c, Action.CONTEST)
    assert o.resolution is Resolution.PARTIAL
    assert o.debit_txn_minor == c.captured_minor - c.refunded_minor
    assert oracle(c)[0] is Action.CONTEST


def test_currency_mismatch_chargeback_exceeds_capture() -> None:
    c = next(c for c in generate_cases(2, 3000) if c.scenario is Scenario.CURRENCY_MISMATCH)
    assert c.chargeback_txn_minor > c.captured_minor


def test_regret_is_never_negative() -> None:
    run = Simulator(generate_cases(4, 1500)).run(Policy(rules=(), default_action=Action.CONTEST))
    assert all(r.regret_usd_minor >= 0 for r in run.results)
