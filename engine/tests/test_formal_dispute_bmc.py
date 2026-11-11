"""Bounded model checking (z3) of the dispute model's money invariants, and the engine
bugs its counterexamples pointed to (as failing tests until each is fixed)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from scruple.formal import dispute as dm
from scruple.formal.bmc import Report, check, induction
from scruple.formal.ops import State
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    FULL_MIX,
    CardDispute,
    DisputeEngine,
    Represent,
    generate_cases,
)
from scruple.sim.evidence import EvidenceDocument
from scruple.sim.models import Stage

FAST_DEPTH = 8  # the slow suite and `make formal` go to scruple.formal.run's depth
SAFE = tuple(i.name for i in dm.INVARIANTS)


@pytest.fixture(scope="module")
def report() -> Report:
    return check(dm.MODEL, FAST_DEPTH)


@pytest.mark.parametrize("name", SAFE)
def test_invariant_holds_to_depth(report: Report, name: str) -> None:
    r = next(x for x in report.results if x.invariant == name)
    assert r.holds, r.counterexample.text() if r.counterexample else name
    assert r.depth == FAST_DEPTH


def test_the_check_is_not_vacuous(report: Report) -> None:
    """Every command fires on some path within the depth."""
    assert all(d is not None for d in report.covered.values()), report.covered


def test_invariants_are_inductive() -> None:
    """One step from any state satisfying them keeps them: they hold at every depth
    (under the abstraction), not only to the bounded depth."""
    ind = induction(dm.MODEL, candidates=SAFE)
    assert set(ind.inductive) == set(SAFE), ind.dropped


def test_a_broken_invariant_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checker finds a planted bug: a reversal that forgets to update `returned`."""
    real = dm._to_merchant

    def leaky(s: State, ops: object, amount: object, day: object) -> State:
        return {**real(s, ops, amount, day), "returned": s["returned"]}

    monkeypatch.setattr(dm, "_to_merchant", leaky)
    rep = check(dm.MODEL, 4, only=("conservation",))
    r = rep.results[0]
    assert not r.holds and r.counterexample is not None
    # the z3 trace is a real run of the Python model, not a solver artefact
    _, broken = r.counterexample.replay(dm.MODEL)
    assert "conservation" in broken


# --- engine bugs from the counterexamples ---------------------------------------------
class MerchantPrevails:
    """An issuer that sides with the merchant at every decision."""

    def merchant_prevails(
        self, case: CardDispute, stage: Stage, evidence: tuple[EvidenceDocument, ...]
    ) -> bool:
        return True


def test_debit_on_top_of_a_settled_refund_is_flagged() -> None:
    """Counterexample (depth 0): a partial dispute whose refund of the disputed amount
    settled before filing opens as a chargeback. The debit posts on top of the refund
    (the network forces it), so the cardholder holds the dispute twice, but because
    that is still within the capture the ledger recorded no violation and the engine
    no incident. A generated case (FULL_MIX, seed 3) does exactly this."""
    case = generate_cases(seed=3, n=106, mix=FULL_MIX)[105]
    rf = case.refund_in_flight
    assert rf is not None and rf.settles_on <= case.opened_on
    assert case.amount_minor * 2 <= case.captured_minor
    clock = ManualClock()
    clock.set_date(case.opened_on)
    eng = DisputeEngine([case], clock)
    assert eng.ledger.returned(case.txn_id) == 2 * case.amount_minor
    assert [i.kind for i in eng.incidents] == ["double_refund"]


def split_race_case() -> CardDispute:
    """A split order: one parcel delivered and not disputed, one delivered and
    disputed, one lost; a partial refund settles after the issuer decides."""
    base = generate_cases(
        seed=7,
        n=112,
        mix=FULL_MIX.model_copy(update={"p_split_shipment": 1.0, "p_refund_race": 1.0}),
    )[111]
    s1, s2, s3 = base.shipments
    ships = (s1.model_copy(update={"disputed": False}), s2, s3)
    amount = s2.amount_minor + s3.amount_minor
    data = base.model_dump()
    data.update(
        shipments=[x.model_dump() for x in ships],
        amount_minor=amount,
        refund_in_flight={
            "amount_minor": 12_000,
            "settles_on": base.opened_on + timedelta(days=30),
        },
    )
    data["truth"]["liability_minor"] = s2.amount_minor
    return CardDispute.model_validate(data)


def test_racing_refund_does_not_pay_a_partly_won_dispute_twice() -> None:
    """Counterexample (depth 3): represent, the issuer awards the merchant part of a
    split order and reverses that part, then a refund in flight settles. The engine
    blocked such a refund only while the chargeback still returned the whole dispute,
    so the cardholder kept the lost parcel's chargeback and got the refund too."""
    case = split_race_case()
    clock = ManualClock()
    clock.set_date(case.opened_on)
    eng = DisputeEngine([case], clock, issuer=MerchantPrevails())
    proofs = tuple(d.doc_id for d in case.documents if d.kind.value == "delivery_proof")
    assert eng.act(case.case_id, Represent(evidence_ids=proofs)).ok
    clock.advance(days=45)
    eng.sync()
    out = eng.outcome(case.case_id)
    assert out is not None and out.resolution.value == "partial"
    assert eng.ledger.returned(case.txn_id) <= case.amount_minor
    assert out.cardholder_net_minor == case.truth.liability_minor
    assert [i.kind for i in eng.incidents] == ["refund_blocked"]
