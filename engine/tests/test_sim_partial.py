from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.domain import ReasonCode
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    Accept,
    CardDispute,
    CaseMix,
    DisputeEngine,
    Represent,
    drain,
    generate_cases,
)
from scruple.sim.evidence import EvidenceDocument, EvidenceKind, document_problems
from scruple.sim.families import FAMILY_WEIGHTS
from scruple.sim.models import Resolution, Stage

SPLIT = CaseMix(p_split_shipment=1.0, family_weights=FAMILY_WEIGHTS)
PARTIAL = CaseMix(p_partial_dispute=1.0)


class AlwaysPrevails:
    def merchant_prevails(
        self, case: CardDispute, stage: Stage, evidence: tuple[EvidenceDocument, ...]
    ) -> bool:
        return True


def split_cases(seed: int, n: int = 400) -> list[CardDispute]:
    return [c for c in generate_cases(seed, n, SPLIT) if c.shipments]


def engine_for(c: CardDispute) -> tuple[DisputeEngine, ManualClock]:
    clock = ManualClock()
    clock.set_date(c.opened_on)
    return DisputeEngine([c], clock, issuer=AlwaysPrevails()), clock


def test_only_not_received_codes_split() -> None:
    cases = generate_cases(1, 600, SPLIT)
    split = [c for c in cases if c.shipments]
    assert split
    for c in cases:
        if c.shipments:
            assert c.spec.legacy_family is ReasonCode.NOT_RECEIVED
        elif c.spec.legacy_family is ReasonCode.NOT_RECEIVED:
            pytest.fail("p_split_shipment=1 must split every not-received order")


@settings(max_examples=30, deadline=None)
@given(seed=st.integers(0, 5_000))
def test_split_orders_are_consistent(seed: int) -> None:
    for c in split_cases(seed, 200):
        assert 2 <= len(c.shipments) <= 3 or len(c.shipments) == 1
        assert sum(s.amount_minor for s in c.shipments) == c.captured_minor
        assert sum(s.amount_minor for s in c.shipments if s.disputed) == c.amount_minor
        lost = sum(s.amount_minor for s in c.shipments if s.disputed and s.delivered_on is None)
        assert c.truth.liability_minor == lost
        if c.truth.claim_valid:
            assert any(s.delivered_on is None for s in c.shipments)
        else:
            assert all(s.delivered_on is not None for s in c.shipments) and lost == 0
        proofs = [d for d in c.documents if d.kind is EvidenceKind.DELIVERY_PROOF]
        assert {d.fields["shipment_id"] for d in proofs} == {
            s.shipment_id for s in c.shipments if s.delivered_on is not None
        }
        for d in proofs:
            assert document_problems(d, c.captured_minor, c.currency, c.processed_on) == ()


def test_overclaim_is_won_only_for_delivered_parcels() -> None:
    c = next(
        x
        for x in split_cases(2)
        if x.truth.claim_valid
        and x.opening_stage is Stage.CHARGEBACK
        and 0 < x.truth.liability_minor < x.amount_minor
    )
    e, clock = engine_for(c)
    assert e.act(c.case_id, Represent(evidence_ids=c.evidence_ids)).ok
    drain(e, clock)
    out = e.outcome(c.case_id)
    assert out is not None and out.resolution is Resolution.PARTIAL
    assert out.cardholder_net_minor == c.truth.liability_minor and out.correct


def test_no_delivery_proof_wins_nothing_on_a_split_order() -> None:
    c = next(x for x in split_cases(3) if x.opening_stage is Stage.CHARGEBACK)
    e, clock = engine_for(c)
    non_delivery = tuple(d.doc_id for d in c.documents if d.kind is not EvidenceKind.DELIVERY_PROOF)
    e.act(c.case_id, Represent(evidence_ids=non_delivery))
    clock.advance(days=10)
    e.sync()
    assert e.observe(c.case_id).stage is Stage.PRE_ARBITRATION or e.is_resolved(c.case_id)
    out_entries = [x.kind for x in e.ledger.entries]
    assert "chargeback_reversal" not in out_entries


@settings(max_examples=40, deadline=None)
@given(seed=st.integers(0, 5_000), data=st.data())
def test_reversal_equals_the_proven_disputed_parcels(seed: int, data: st.DataObject) -> None:
    cases = [c for c in split_cases(seed, 150) if c.opening_stage is Stage.CHARGEBACK]
    if not cases:
        return
    c = data.draw(st.sampled_from(cases))
    subset = data.draw(st.lists(st.sampled_from(c.evidence_ids), unique=True))
    e, clock = engine_for(c)
    e.act(c.case_id, Represent(evidence_ids=tuple(subset)))
    drain(e, clock)
    proven = {
        d.fields.get("shipment_id")
        for d in c.documents
        if d.doc_id in subset and d.kind is EvidenceKind.DELIVERY_PROOF
    }
    expected = sum(s.amount_minor for s in c.shipments if s.disputed and s.shipment_id in proven)
    assert e.ledger.returned(c.txn_id) == c.amount_minor - expected
    assert all(v == 0 for v in e.ledger.currency_totals().values())
    assert e.ledger.returned(c.txn_id) >= c.truth.liability_minor


def test_partial_disputes_cover_part_of_the_capture() -> None:
    cases = generate_cases(4, 300, PARTIAL)
    for c in cases:
        assert c.amount_minor < c.captured_minor
        assert c.amount_minor >= c.captured_minor * 20 // 100 - 1
        assert c.truth.liability_minor == (c.amount_minor if c.truth.claim_valid else 0)
    c = next(x for x in cases if x.opening_stage is Stage.CHARGEBACK and x.truth.claim_valid)
    e, _ = engine_for(c)
    e.act(c.case_id, Accept())
    out = e.outcome(c.case_id)
    assert out is not None and out.cardholder_net_minor == c.amount_minor and out.correct
    assert e.ledger.headroom(c.txn_id) == c.captured_minor - c.amount_minor


def test_shipment_validation() -> None:
    c = split_cases(5)[0]
    data = c.model_dump()
    bad = [dict(s) for s in data["shipments"]]
    bad[0]["amount_minor"] += 1
    with pytest.raises(ValueError, match="add up"):
        CardDispute.model_validate({**data, "shipments": bad})
    flip = [dict(s) for s in data["shipments"]]
    flip[0]["disputed"] = not flip[0]["disputed"]
    with pytest.raises(ValueError, match="disputed amount"):
        CardDispute.model_validate({**data, "shipments": flip})


def test_zero_options_leave_default_generation_unchanged() -> None:
    assert generate_cases(6, 200) == generate_cases(
        6, 200, CaseMix(p_split_shipment=0.0, p_partial_dispute=0.0)
    )
