from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import CaseMix, DisputeEngine, generate_cases
from scruple.sim.merchants import (
    MCCS,
    TIER_BPS,
    MerchantProfile,
    MonthStats,
    RiskTier,
    generate_merchants,
)


def test_merchants_are_deterministic() -> None:
    assert generate_merchants(4) == generate_merchants(4)
    assert generate_merchants(4) != generate_merchants(5)


def test_merchant_ids_mcc_and_history() -> None:
    ms = generate_merchants(1, n=25, months=6, end_month=date(2026, 3, 1))
    assert [m.merchant_id for m in ms] == [f"m-{i:03d}" for i in range(1, 26)]
    for m in ms:
        assert m.mcc in MCCS
        assert [h.month for h in m.history] == [
            date(2025, 10, 1),
            date(2025, 11, 1),
            date(2025, 12, 1),
            date(2026, 1, 1),
            date(2026, 2, 1),
            date(2026, 3, 1),
        ]


@given(seed=st.integers(0, 5_000))
def test_ratios_stay_inside_the_tier_band(seed: int) -> None:
    for m in generate_merchants(seed, n=10):
        lo, hi = TIER_BPS[m.tier]
        for h in m.history:
            r = m.chargeback_ratio(h.month)
            assert r <= Decimal(hi) / 10_000
            # integer flooring can only push the ratio down, by under one chargeback
            assert r >= (Decimal(lo) / 10_000) - Decimal(1) / h.settled_txns
            assert h.fraud_reports <= h.chargebacks


def test_all_tiers_appear_in_a_large_population() -> None:
    assert {m.tier for m in generate_merchants(2, n=200)} == set(RiskTier)


def test_ratio_defaults_to_latest_and_handles_missing_months() -> None:
    m = generate_merchants(3, n=1)[0]
    last = m.history[-1]
    assert m.chargeback_ratio() == Decimal(last.chargebacks) / Decimal(last.settled_txns)
    assert m.chargeback_ratio(date(1999, 1, 1)) == 0
    assert m.total_chargebacks == sum(h.chargebacks for h in m.history)


def test_month_stats_validation() -> None:
    with pytest.raises(ValidationError, match="first day"):
        MonthStats(month=date(2026, 1, 2), settled_txns=1, chargebacks=0, fraud_reports=0)
    with pytest.raises(ValidationError, match="more chargebacks"):
        MonthStats(month=date(2026, 1, 1), settled_txns=1, chargebacks=2, fraud_reports=0)
    h = MonthStats(month=date(2026, 1, 1), settled_txns=0, chargebacks=0, fraud_reports=0)
    m = MerchantProfile(merchant_id="m-x", mcc="5999", tier=RiskTier.LOW, history=(h,))
    assert m.chargeback_ratio() == 0
    with pytest.raises(ValidationError, match="chronological"):
        MerchantProfile(merchant_id="m-x", mcc="5999", tier=RiskTier.LOW, history=(h, h))
    with pytest.raises(ValidationError):
        MerchantProfile(merchant_id="m-x", mcc="59", tier=RiskTier.LOW, history=(h,))


def test_cases_use_generated_merchants() -> None:
    mix = CaseMix(n_merchants=7)
    merchants = {m.merchant_id: m for m in generate_merchants(9, 7)}
    cases = generate_cases(9, 200, mix)
    for c in cases:
        assert c.merchant_id in merchants
        assert c.mcc == merchants[c.merchant_id].mcc
    clock = ManualClock()
    clock.set_date(cases[0].opened_on)
    e = DisputeEngine(cases, clock, merchants=tuple(merchants.values()))
    obs = e.observe(cases[0].case_id)
    assert obs.merchant == merchants[cases[0].merchant_id]
