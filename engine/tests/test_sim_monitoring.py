from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.sim.card_ledger import CardLedger
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import DisputeEngine
from scruple.sim.merchants import MerchantProfile, MonthStats, RiskTier, generate_merchants
from scruple.sim.monitoring import (
    ECM_MIN,
    ECM_RATIO,
    HECM_MIN,
    HECM_RATIO,
    VAMP_FEE_PER_DISPUTE_USD,
    VAMP_MIN_COUNT,
    VAMP_RATIO,
    Level,
    Program,
    evaluate,
    evaluate_ecm,
    evaluate_vamp,
    in_force,
    post_fines,
)
from tests.test_sim_networks import source_ids


def month(y: int, m: int, settled: int, cbs: int, fraud: int = 0) -> MonthStats:
    return MonthStats(
        month=date(y, m, 1), settled_txns=settled, chargebacks=cbs, fraud_reports=fraud
    )


def profile(*months: MonthStats) -> MerchantProfile:
    return MerchantProfile(merchant_id="m-x", mcc="5999", tier=RiskTier.HIGH, history=months)


def _add(y: int, m: int, k: int) -> tuple[int, int]:
    q, r = divmod(m - 1 + k, 12)
    return y + q, r + 1


def flat(n: int, settled: int, cbs: int, start: tuple[int, int] = (2026, 1)) -> MerchantProfile:
    return profile(*(month(*_add(*start, k), settled, cbs) for k in range(n)))


@pytest.mark.parametrize(
    "rules", [VAMP_RATIO, VAMP_FEE_PER_DISPUTE_USD, ECM_MIN, ECM_RATIO, HECM_MIN, HECM_RATIO]
)
def test_every_rule_cites_a_source(rules: tuple[object, ...]) -> None:
    for r in rules:
        assert getattr(r, "source") in source_ids()  # noqa: B009
    assert VAMP_MIN_COUNT.source in source_ids()


def test_in_force_picks_the_latest_effective_rule() -> None:
    assert in_force(VAMP_RATIO, date(2025, 5, 1)) is None
    assert in_force(VAMP_RATIO, date(2025, 6, 1)).value == Decimal("0.022")  # type: ignore[union-attr]
    assert in_force(VAMP_RATIO, date(2026, 3, 1)).value == Decimal("0.022")  # type: ignore[union-attr]
    assert in_force(VAMP_RATIO, date(2026, 4, 1)).value == Decimal("0.015")  # type: ignore[union-attr]


# ------------------------------------------------------------------ VAMP
@pytest.mark.parametrize(
    ("y", "m", "settled", "cbs", "fraud", "level"),
    [
        (2025, 5, 50_000, 1_000, 1_000, Level.NONE),  # before the 2.2% threshold
        (2025, 6, 90_000, 1_000, 980, Level.EXCESSIVE),  # 1,980 / 90,000 = 2.2%
        (2025, 6, 90_000, 1_000, 979, Level.NONE),  # just under 2.2%
        (2026, 3, 100_000, 1_000, 800, Level.NONE),  # 1.8% < 2.2%
        (2026, 4, 100_000, 1_000, 800, Level.EXCESSIVE),  # 1.8% >= 1.5%
        (2026, 4, 50_000, 800, 699, Level.NONE),  # 1,499 events: under the minimum
        (2026, 4, 50_000, 800, 700, Level.EXCESSIVE),  # 1,500 events
    ],
)
def test_vamp_thresholds(y: int, m: int, settled: int, cbs: int, fraud: int, level: Level) -> None:
    (s,) = evaluate_vamp(profile(month(y, m, settled, cbs, fraud)))
    assert s.level is level
    assert s.count == cbs + fraud
    assert s.ratio == Decimal(cbs + fraud) / settled


def test_vamp_fee_starts_in_october_2025() -> None:
    sep, octo = evaluate_vamp(
        profile(month(2025, 9, 50_000, 1_000, 1_000), month(2025, 10, 50_000, 1_000, 1_000))
    )
    assert sep.level is octo.level is Level.EXCESSIVE
    assert sep.fine_usd_minor == 0
    assert octo.fine_usd_minor == 800 * 1_000
    assert octo.month_in_program == 2


# ------------------------------------------------------------------ ECM
@pytest.mark.parametrize(
    ("cbs", "settled", "level"),
    [
        (99, 1_000, Level.NONE),
        (100, 6_667, Level.NONE),  # 1.4999%: under 1.5%
        (100, 6_666, Level.ECM),
        (300, 10_000, Level.HECM),  # exactly 3%
        (300, 10_001, Level.ECM),
        (299, 1_000, Level.ECM),
    ],
)
def test_ecm_levels(cbs: int, settled: int, level: Level) -> None:
    (s,) = evaluate_ecm(profile(month(2026, 1, settled, cbs)))
    assert s.level is level


def test_ecm_ratio_uses_the_previous_months_volume() -> None:
    _, b = evaluate_ecm(profile(month(2026, 1, 10_000, 0), month(2026, 2, 5_000, 120)))
    assert b.ratio == Decimal(120) / 10_000  # 1.2%: below 1.5% despite 2.4% this month
    assert b.level is Level.NONE


@pytest.mark.parametrize(
    ("cbs", "settled", "fines"),
    [
        (150, 5_000, [0, 100_000, 100_000, 100_000]),
        (400, 5_000, [0, 100_000, 200_000, 200_000]),
    ],
)
def test_fine_schedule(cbs: int, settled: int, fines: list[int]) -> None:
    out = evaluate_ecm(flat(4, settled, cbs))
    assert [s.fine_usd_minor for s in out] == fines
    assert [s.month_in_program for s in out] == [1, 2, 3, 4]


def test_exit_after_three_months_below_and_re_entry_restarts() -> None:
    hist = [month(2025, 1, 5_000, 150)]
    hist += [month(2025, m, 5_000, 10) for m in (2, 3, 4)]
    hist += [month(2025, 5, 5_000, 150), month(2025, 6, 5_000, 150)]
    out = evaluate_ecm(profile(*hist))
    assert [s.month_in_program for s in out] == [1, 1, 1, 0, 1, 2]
    assert [s.fine_usd_minor for s in out] == [0, 0, 0, 0, 0, 100_000]


def test_two_months_below_do_not_exit() -> None:
    hist = [month(2025, 1, 5_000, 150), month(2025, 2, 5_000, 1), month(2025, 3, 5_000, 1)]
    hist.append(month(2025, 4, 5_000, 150))
    out = evaluate_ecm(profile(*hist))
    assert out[-1].month_in_program == 2 and out[-1].fine_usd_minor == 100_000


# ------------------------------------------------------------------ ledger
def test_fines_post_balanced_entries() -> None:
    statuses = evaluate(flat(3, 5_000, 400))
    ledger = CardLedger()
    n = post_fines(ledger, statuses)
    assert n == 2
    assert [e.kind for e in ledger.entries] == ["fine:mastercard_ecm"] * 2
    assert ledger.balance("merchant:m-x", "USD") == -300_000
    assert ledger.balance("network:fines", "USD") == 300_000
    assert all(v == 0 for v in ledger.currency_totals().values())


def test_engine_posts_monitoring_fines_once() -> None:
    hot = flat(3, 5_000, 400).model_copy(update={"merchant_id": "m-hot"})
    e = DisputeEngine([], ManualClock(), merchants=(hot,))
    first = e.post_monitoring_fines()
    assert e.post_monitoring_fines() == first
    fines = [x for x in e.ledger.entries if x.kind.startswith("fine:")]
    assert len(fines) == 2
    assert e.ledger.balance("merchant:m-hot", "USD") == -300_000


def test_charges_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        CardLedger().merchant_charge("fine:x", "m", 0, "network:fines", ref="r")


@given(seed=st.integers(0, 3_000))
def test_generated_merchants_evaluate_cleanly(seed: int) -> None:
    for p in generate_merchants(seed, n=8, months=6):
        statuses = evaluate(p)
        assert len(statuses) == 2 * len(p.history)
        assert {s.program for s in statuses} == set(Program)
        for s in statuses:
            assert s.fine_usd_minor >= 0
            if s.level is Level.NONE:
                assert s.fine_usd_minor == 0
            assert s.ratio >= 0
