from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.sim.card_ledger import CardLedger
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    Accept,
    AgentAction,
    CardDispute,
    CaseMix,
    DisputeEngine,
    Refund,
    Represent,
    TruthIssuer,
    drain,
    generate_cases,
)
from scruple.sim.evidence import document_problems
from scruple.sim.fx import BASE_USD_PER_UNIT, CURRENCIES, EXPONENT, MAX_DRIFT, FxTable
from scruple.sim.models import Resolution, Stage

FX = FxTable()
FOREIGN = CaseMix(p_foreign_currency=1.0)
days = st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31))
ccys = st.sampled_from(CURRENCIES)


def test_minor_unit_exponents() -> None:
    assert EXPONENT == {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}


@pytest.mark.parametrize("ccy", CURRENCIES)
def test_same_currency_is_identity(ccy: str) -> None:
    assert FX.convert(12_345, ccy, ccy, date(2026, 1, 1)) == 12_345
    assert FX.rate(ccy, ccy, date(2026, 1, 1)) == 1
    assert FX.round_trip_bound(ccy, ccy, date(2026, 1, 1)) == 0


@given(ccy=ccys, day=days)
def test_rates_are_decimal_bounded_and_deterministic(ccy: str, day: date) -> None:
    r = FX.usd_per_unit(ccy, day)
    assert isinstance(r, Decimal)
    base = BASE_USD_PER_UNIT[ccy]
    assert base * (1 - MAX_DRIFT) - Decimal("1e-8") <= r <= base * (1 + MAX_DRIFT) + Decimal("1e-8")
    assert r == FxTable().usd_per_unit(ccy, day)
    exp = r.as_tuple().exponent
    assert isinstance(exp, int) and exp >= -8


def test_rates_move_by_day_and_seed() -> None:
    d = date(2026, 3, 1)
    assert FX.usd_per_unit("EUR", d) != FX.usd_per_unit("EUR", d + timedelta(days=1))
    assert FX.usd_per_unit("EUR", d) != FxTable(seed=9).usd_per_unit("EUR", d)


@settings(max_examples=300)
@given(x=st.integers(0, 10**12), a=ccys, b=ccys, day=days)
def test_round_trip_within_the_rounding_bound(x: int, a: str, b: str, day: date) -> None:
    back = FX.convert(FX.convert(x, a, b, day), b, a, day)
    assert abs(back - x) <= FX.round_trip_bound(a, b, day)


@given(x=st.integers(0, 10**10), y=st.integers(0, 10**10), a=ccys, b=ccys, day=days)
def test_conversion_is_monotone(x: int, y: int, a: str, b: str, day: date) -> None:
    lo, hi = min(x, y), max(x, y)
    assert FX.convert(lo, a, b, day) <= FX.convert(hi, a, b, day)


def test_half_even_rounding_to_minor_units() -> None:
    # USD -> JPY drops two decimal places: exact halves go to the even yen
    d = date(2026, 1, 1)
    k = FX.minor_factor("USD", "JPY", d)
    assert FX.convert(1, "USD", "JPY", d) == int((Decimal(1) * k).to_integral_value())


def test_bound_matches_the_formula() -> None:
    d = date(2026, 1, 1)
    assert FX.round_trip_bound("USD", "EUR", d) == 1  # k close to 1: floor(0.5 + ~0.54)
    assert FX.round_trip_bound("JPY", "USD", d) == 1  # yen -> cents loses up to ~0.75 yen
    assert FX.round_trip_bound("USD", "JPY", d) == 0  # a cent survives a trip through yen


# ----------------------------------------------------------------- ledger
def test_ledger_posts_in_settlement_currency_and_checks_in_txn_currency() -> None:
    lg = CardLedger(FX)
    d0, d1 = date(2026, 1, 5), date(2026, 2, 5)
    lg.capture("t", "m", 10_000, "EUR", "USD", d0)
    assert lg.entries[0].currency == "USD"
    assert lg.balance("merchant:m", "USD") == FX.convert(10_000, "EUR", "USD", d0)
    lg.refund("t", "c", 10_000, d1)
    assert lg.headroom("t") == 0  # exact in EUR, whatever the USD amounts were
    lg.chargeback_debit("t", "c", 10_000, d1)
    assert lg.violations and lg.over_credited() == {"t": 10_000}
    assert all(v == 0 for v in lg.currency_totals().values())


def test_ledger_needs_a_date_to_convert() -> None:
    lg = CardLedger(FX)
    with pytest.raises(ValueError, match="posting date"):
        lg.capture("t", "m", 100, "EUR", "USD")


# ----------------------------------------------------------------- engine
def foreign_case(stage: Stage = Stage.CHARGEBACK) -> CardDispute:
    return next(
        c
        for c in generate_cases(3, 200, FOREIGN)
        if c.opening_stage is stage and not c.truth.claim_valid
    )


def test_generator_prices_foreign_orders() -> None:
    cases = generate_cases(1, 300, FOREIGN)
    assert {c.currency for c in cases} == {"EUR", "GBP", "JPY"}
    assert {c.settlement_currency for c in cases} == {"USD"}
    for c in cases:
        for d in c.documents:
            assert d.fields.get("currency", c.currency) == c.currency
            assert document_problems(d, c.captured_minor, c.currency, c.processed_on) == ()
    assert all(c.currency == "USD" for c in generate_cases(1, 100))


def test_reversal_at_a_later_rate_leaves_an_fx_difference() -> None:
    c = foreign_case()
    clock = ManualClock()
    clock.set_date(c.opened_on)
    e = DisputeEngine([c], clock, issuer=TruthIssuer())
    e.act(c.case_id, Represent())
    drain(e, clock)
    out = e.outcome(c.case_id)
    assert out is not None and out.resolution is Resolution.WON and out.correct
    debit_day = c.opened_on
    reversal_day = c.opened_on + timedelta(days=10)
    debit = FX.convert(c.amount_minor, c.currency, "USD", debit_day)
    credit = FX.convert(c.amount_minor, c.currency, "USD", reversal_day)
    assert out.settlement_currency == "USD"
    assert out.merchant_settled_minor == credit - debit
    assert out.cardholder_net_minor == 0


def test_accept_debits_at_the_chargeback_day_rate() -> None:
    c = foreign_case()
    clock = ManualClock()
    clock.set_date(c.opened_on)
    e = DisputeEngine([c], clock)
    e.act(c.case_id, Accept())
    out = e.outcome(c.case_id)
    assert out is not None
    assert out.merchant_settled_minor == -FX.convert(c.amount_minor, c.currency, "USD", c.opened_on)


def test_unsupported_currency_rejected() -> None:
    c = foreign_case()
    with pytest.raises(ValueError, match="unsupported currency"):
        CardDispute.model_validate({**c.model_dump(), "currency": "XXX"})


ACTIONS: list[AgentAction] = [Accept(), Represent(), Refund(amount_minor=100)]


@settings(max_examples=40, deadline=None)
@given(
    seed=st.integers(0, 5_000),
    plan=st.lists(
        st.tuples(st.integers(0, 5), st.sampled_from(ACTIONS), st.integers(0, 15)), max_size=20
    ),
)
def test_multi_currency_ledger_balances_after_any_actions(
    seed: int, plan: list[tuple[int, AgentAction, int]]
) -> None:
    mix = CaseMix(p_foreign_currency=0.7, p_refund_race=0.5, p_partial_dispute=0.3)
    cases = sorted(generate_cases(seed, 6, mix), key=lambda c: c.opened_on)
    clock = ManualClock()
    clock.set_date(cases[0].opened_on)
    e = DisputeEngine(cases, clock)
    for idx, action, wait in plan:
        e.act(cases[idx].case_id, action)
        clock.advance(days=wait)
    drain(e, clock)
    assert all(v == 0 for v in e.ledger.currency_totals().values())
    for entry in e.ledger.entries:
        assert sum(p.amount_minor for p in entry.postings) == 0
        assert entry.currency == "USD"  # every case settles in USD
    assert set(e.ledger.over_credited()) <= {v.txn_id for v in e.ledger.violations}
