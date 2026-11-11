"""AML typologies (structuring, mule rings, rapid movement), labels and indicators."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.sim.aml.generator import generate_aml_case
from scruple.sim.aml.indicators import (
    INDICATORS,
    Indicator,
    assess,
    cycles,
    holds,
    near_threshold,
    new_share,
    passes_through,
    reportable_cash,
)
from scruple.sim.aml.models import (
    BENIGN,
    CASH,
    CTR_THRESHOLD_MINOR,
    FAMILIES,
    TYPOLOGIES,
    AlertRule,
    AmlCase,
    AmlFamily,
    Transfer,
)

F = AmlFamily
TWIN_RULES = {
    F.STRUCTURING: {AlertRule.CASH_NEAR_THRESHOLD},
    F.CASH_BUSINESS: {AlertRule.CASH_NEAR_THRESHOLD},
    F.MULE_RING: {AlertRule.FAN_IN, AlertRule.FAN_OUT},
    F.MARKETPLACE_SELLER: {AlertRule.FAN_IN},
    F.PAYROLL: {AlertRule.FAN_OUT},
    F.RAPID_MOVEMENT: {AlertRule.PASS_THROUGH},
    F.PROPERTY_PURCHASE: {AlertRule.PASS_THROUGH},
}


def read(case: AmlCase) -> tuple[bool, tuple[Indicator, ...]]:
    linked = [case.transfer(x) for x in case.alert.linked_txn_ids]
    accounts = {a.account_id: a for a in case.accounts}
    return assess(case.alert.rule, case.subject, accounts, linked, case.alert.raised_on)


def test_three_typologies_each_with_lookalikes() -> None:
    assert {f.value for f in TYPOLOGIES} == {"structuring", "mule_ring", "rapid_movement"}
    assert len(BENIGN) == 4 and set(FAMILIES) == TYPOLOGIES | BENIGN
    for typ in TYPOLOGIES:  # every rule a typology trips is also tripped by a lookalike
        for rule in TWIN_RULES[typ]:
            assert any(rule in TWIN_RULES[b] for b in BENIGN), (typ, rule)


@pytest.mark.parametrize(
    ("amount", "reportable"),
    [
        (CTR_THRESHOLD_MINOR - 1, False),
        (CTR_THRESHOLD_MINOR, False),
        (CTR_THRESHOLD_MINOR + 1, True),
    ],
)
def test_ctr_threshold_is_more_than_ten_thousand_dollars(amount: int, reportable: bool) -> None:
    t = Transfer(
        txn_id="T-1",
        at=datetime(2026, 3, 2, 10),
        src=CASH,
        dst="A-1",
        amount_minor=amount,
        channel="cash",
    )
    assert reportable_cash(t) is reportable
    assert near_threshold(t) is not reportable


@given(seed=st.integers(0, 5_000), family=st.sampled_from(FAMILIES), d=st.floats(0, 1))
@settings(max_examples=150, deadline=None)
def test_labels_and_rules(seed: int, family: AmlFamily, d: float) -> None:
    c = generate_aml_case(seed, family, d)
    assert c.alert.rule in TWIN_RULES[family]
    typ = set(c.truth.typology_txn_ids)
    if family in TYPOLOGIES:
        assert typ and typ <= set(c.alert.linked_txn_ids)
    else:
        assert not typ


@given(seed=st.integers(0, 5_000), family=st.sampled_from(FAMILIES), d=st.floats(0, 1))
@settings(max_examples=200, deadline=None)
def test_observable_reading_separates_every_typology_from_its_lookalike(
    seed: int, family: AmlFamily, d: float
) -> None:
    c = generate_aml_case(seed, family, d)
    suspicious, held = read(c)
    assert suspicious == c.truth.suspicious, (family, held)


@pytest.mark.parametrize("seed", range(30))
def test_structuring_avoids_every_ctr(seed: int) -> None:
    c = generate_aml_case(seed, "structuring", seed / 29)
    deposits = [c.transfer(x) for x in c.truth.typology_txn_ids]
    assert 3 <= len(deposits) <= 6
    assert all(t.channel == "cash" and near_threshold(t) for t in deposits)
    assert sum(t.amount_minor for t in deposits) > CTR_THRESHOLD_MINOR  # it adds up
    assert {"near_threshold_cash", "profile_mismatch"} <= {i.value for i in read(c)[1]}


@pytest.mark.parametrize("seed", range(30))
def test_mule_rings_pass_money_through_new_accounts(seed: int) -> None:
    c = generate_aml_case(seed, "mule_ring", seed / 29)
    txns = [c.transfer(x) for x in c.truth.typology_txn_ids]
    accounts = {a.account_id: a for a in c.accounts}
    fan = Indicator.FAN_IN if c.alert.rule is AlertRule.FAN_IN else Indicator.FAN_OUT
    assert holds(fan, c.subject, accounts, txns, c.alert.raised_on)
    assert passes_through(c.subject_id, txns)
    assert new_share(c.subject_id, accounts, txns, c.alert.raised_on) >= 0.6


@pytest.mark.parametrize("seed", range(30))
def test_rapid_movement_has_repeated_cycles_and_property_one(seed: int) -> None:
    rapid = generate_aml_case(seed, "rapid_movement", seed / 29)
    assert cycles(rapid.subject_id, [rapid.transfer(x) for x in rapid.truth.typology_txn_ids]) >= 2
    prop = generate_aml_case(seed, "property_purchase", seed / 29)
    linked = [prop.transfer(x) for x in prop.alert.linked_txn_ids]
    assert cycles(prop.subject_id, linked) <= 1


def test_pass_through_needs_timely_outflows() -> None:
    t0 = datetime(2026, 3, 2, 9)

    def tx(i: int, src: str, dst: str, amt: int, hours: int) -> Transfer:
        return Transfer(
            txn_id=f"T-{i}",
            at=t0 + timedelta(hours=hours),
            src=src,
            dst=dst,
            amount_minor=amt,
            channel="wire",
        )

    fast = [tx(1, "X-1", "A-1", 100_000, 0), tx(2, "A-1", "X-2", 90_000, 70)]
    slow = [tx(1, "X-1", "A-1", 100_000, 0), tx(2, "A-1", "X-2", 90_000, 73)]
    small = [tx(1, "X-1", "A-1", 100_000, 0), tx(2, "A-1", "X-2", 70_000, 5)]
    assert passes_through("A-1", fast) and cycles("A-1", fast) == 0  # under $5,000 in
    assert not passes_through("A-1", slow) and not passes_through("A-1", small)
    big = [tx(1, "X-1", "A-1", 600_000, 0), tx(2, "A-1", "X-2", 590_000, 2)]
    assert cycles("A-1", big) == 1


def test_every_indicator_is_false_on_no_citations() -> None:
    c = generate_aml_case(1, "mule_ring")
    accounts = {a.account_id: a for a in c.accounts}
    assert not any(holds(i, c.subject, accounts, [], c.alert.raised_on) for i in INDICATORS)
