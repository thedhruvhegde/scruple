from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    Accept,
    CardDispute,
    DisputeEngine,
    Escalate,
    Represent,
    Truth,
    drain,
)
from scruple.sim.fees import DEFAULT_FEES, NO_FEES, FeeSchedule
from scruple.sim.models import Resolution, Stage
from scruple.sim.networks import Network


def case(
    network: Network = Network.VISA,
    code: str = "13.1",
    valid: bool = True,
    stage: Stage = Stage.CHARGEBACK,
    amount: int = 10_000,
) -> CardDispute:
    return CardDispute(
        case_id="cd-1",
        txn_id="tx-1",
        merchant_id="m-001",
        network=network,
        reason_code=code,
        currency="USD",
        amount_minor=amount,
        captured_minor=amount,
        processed_on=date(2026, 1, 5),
        opened_on=date(2026, 2, 2),
        opening_stage=stage,
        truth=Truth(claim_valid=valid, liability_minor=amount if valid else 0),
    )


def engine(
    c: CardDispute, fees: dict[Network, FeeSchedule] | None = None
) -> tuple[DisputeEngine, ManualClock]:
    clock = ManualClock()
    clock.set_date(c.opened_on)
    return DisputeEngine([c], clock, fees=fees), clock


def fee_kinds(e: DisputeEngine) -> list[str]:
    return [x.kind for x in e.ledger.entries if x.kind.startswith("fee:")]


def test_fee_schedule_rejects_negative_and_terminal_fees() -> None:
    with pytest.raises(ValidationError, match="negative"):
        FeeSchedule(network=Network.VISA, on_enter={Stage.CHARGEBACK: -1}, arbitration_loss=0)
    with pytest.raises(ValidationError, match="no fee"):
        FeeSchedule(network=Network.VISA, on_enter={Stage.RESOLVED: 1}, arbitration_loss=0)
    with pytest.raises(ValidationError):
        FeeSchedule(network=Network.VISA, arbitration_loss=-5)


@pytest.mark.parametrize("network", list(Network))
def test_default_fees_are_labelled_assumptions(network: Network) -> None:
    sched = DEFAULT_FEES[network]
    assert sched.assumption and "A-17" in sched.basis
    assert sched.network is network


def test_chargeback_fee_posted_on_opening() -> None:
    e, _ = engine(case())
    assert fee_kinds(e) == ["fee:chargeback"]
    fee = next(x for x in e.ledger.entries if x.kind == "fee:chargeback")
    assert fee.currency == "USD"
    assert {p.account: p.amount_minor for p in fee.postings} == {
        "merchant:m-001": -DEFAULT_FEES[Network.VISA].entry_fee(Stage.CHARGEBACK),
        "processor:fees": DEFAULT_FEES[Network.VISA].entry_fee(Stage.CHARGEBACK),
    }


def test_inquiry_carries_no_fee_until_it_becomes_a_chargeback() -> None:
    e, clock = engine(case(stage=Stage.INQUIRY))
    assert fee_kinds(e) == []
    clock.advance(days=30)
    e.sync()
    assert fee_kinds(e) == ["fee:chargeback"]


def test_accept_costs_only_the_chargeback_fee() -> None:
    e, _ = engine(case())
    assert e.act("cd-1", Accept()).ok
    out = e.outcome("cd-1")
    assert out is not None and out.resolution is Resolution.ACCEPTED
    assert out.fees_usd_minor == 1_500
    assert out.cardholder_net_minor == 10_000 and out.correct


def test_contesting_a_valid_claim_to_arbitration_costs_every_fee() -> None:
    e, clock = engine(case(valid=True))
    assert e.act("cd-1", Represent(evidence_ids=())).ok
    clock.advance(days=10)
    e.sync()
    assert e.observe("cd-1").stage is Stage.PRE_ARBITRATION
    assert e.act("cd-1", Escalate()).ok
    drain(e, clock)
    out = e.outcome("cd-1")
    assert out is not None and out.resolution is Resolution.LOST
    assert fee_kinds(e) == ["fee:chargeback", "fee:pre_arbitration", "fee:arbitration_loss"]
    assert out.fees_usd_minor == 1_500 + 2_500 + 60_000
    assert out.path == (
        Stage.CHARGEBACK,
        Stage.REPRESENTMENT,
        Stage.PRE_ARBITRATION,
        Stage.ARBITRATION,
        Stage.RESOLVED,
    )


def test_winning_representment_reverses_the_debit() -> None:
    e, clock = engine(case(valid=False))
    e.act("cd-1", Represent())
    drain(e, clock)
    out = e.outcome("cd-1")
    assert out is not None and out.resolution is Resolution.WON
    assert out.cardholder_net_minor == 0 and out.correct
    assert out.fees_usd_minor == 1_500
    kinds = [x.kind for x in e.ledger.entries]
    assert kinds == ["capture", "chargeback_debit", "fee:chargeback", "chargeback_reversal"]


def test_mastercard_arbitration_loss_fee() -> None:
    e, clock = engine(case(network=Network.MASTERCARD, code="4855"))
    e.act("cd-1", Represent())
    clock.advance(days=10)
    e.act("cd-1", Escalate())
    drain(e, clock)
    out = e.outcome("cd-1")
    assert out is not None
    assert out.fees_usd_minor == 1_500 + 2_500 + 57_500


def test_allocation_charges_pre_arbitration_on_the_merchant_response() -> None:
    e, clock = engine(case(code="10.4", valid=False))
    e.act("cd-1", Represent())
    assert fee_kinds(e) == ["fee:chargeback", "fee:pre_arbitration"]
    drain(e, clock)
    out = e.outcome("cd-1")
    assert out is not None and out.resolution is Resolution.WON


def test_no_fees_schedule_posts_no_fee_entries() -> None:
    e, clock = engine(case(), fees=NO_FEES)
    e.act("cd-1", Represent())
    clock.advance(days=10)
    e.act("cd-1", Escalate())
    drain(e, clock)
    assert fee_kinds(e) == []
    out = e.outcome("cd-1")
    assert out is not None and out.fees_usd_minor == 0
