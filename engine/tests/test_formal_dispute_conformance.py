"""The dispute model in scruple.formal steps exactly like the real DisputeEngine.

Random agents (every action kind, legal or not) drive one-case engines; every engine
step must be an enabled model command whose successor equals the engine's state."""

from __future__ import annotations

import collections
from collections.abc import Iterator
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.formal import dispute as dm
from scruple.formal.conformance import DisputeConformance, run_dispute
from scruple.formal.fx import FxModel, round_half_even, round_sig
from scruple.formal.ops import ModelError
from scruple.sim.disputes import FULL_MIX, CardDispute, CaseMix, Refund, generate_cases
from scruple.sim.fx import CURRENCIES, FxTable

# every generator feature on, at rates high enough to reach races, partial disputes,
# split orders, FX and inquiries often
HARD_MIX = FULL_MIX.model_copy(
    update={
        "p_refund_race": 0.7,
        "p_partial_dispute": 0.4,
        "p_split_shipment": 0.8,
        "p_foreign_currency": 0.4,
        "p_inquiry": 0.4,
    }
)
MIXES: dict[str, CaseMix] = {"full": FULL_MIX, "hard": HARD_MIX}


def _cases(seed: int, n: int, mix: CaseMix) -> list[CardDispute]:
    return generate_cases(seed=seed, n=n, mix=mix)


@pytest.mark.parametrize("mix", sorted(MIXES))
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_engine_steps_are_model_steps(seed: int, mix: str) -> None:
    seen: collections.Counter[str] = collections.Counter()
    for i, case in enumerate(_cases(seed, 250, MIXES[mix])):
        # every invariant of the model also holds on each state the engine reaches
        seen.update(run_dispute(case, i).commands)
    assert sum(seen.values()) > 500


def test_every_model_command_is_exercised() -> None:
    seen: collections.Counter[str] = collections.Counter()
    for i, case in enumerate(_cases(11, 600, HARD_MIX)):
        seen.update(run_dispute(case, i).commands)
    assert set(seen) == {c.name for c in dm.COMMANDS}


def _fee_mutant(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = dm._fee
    monkeypatch.setattr(dm, "_fee", lambda s, ops, a, proc: orig(s, ops, a + 1, proc))


def _deadline_mutant(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = dm.step_due_by
    monkeypatch.setattr(dm, "step_due_by", lambda s, p, ops, d: orig(s, p, ops, d + 1))


def _race_mutant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm, "race_blocked", lambda s, p, ops: p["R"] > p["C"] - s["returned"])


def _reversal_mutant(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = dm._resolve
    monkeypatch.setattr(dm, "_resolve", lambda s, ops, day, res, rev=0: orig(s, ops, day, res, 0))


@pytest.mark.parametrize(
    "mutate",
    [_fee_mutant, _deadline_mutant, _race_mutant, _reversal_mutant],
    ids=lambda f: f.__name__,
)
def test_a_drifted_model_is_caught(mutate: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """The conformance check has teeth: small changes to the model fail it."""
    assert callable(mutate)
    mutate(monkeypatch)
    cases = _cases(11, 300, HARD_MIX)

    def run_all() -> None:
        for i, case in enumerate(cases):
            run_dispute(case, i, invariants=False)

    with pytest.raises(ModelError):
        run_all()


def test_refusals_agree_with_the_model() -> None:
    case = next(c for c in _cases(5, 50, FULL_MIX) if c.opening_stage.value == "chargeback")
    conf = DisputeConformance(case, invariants=False)
    head = case.captured_minor - conf.s["returned"]
    assert not conf.act(Refund(amount_minor=head + 1))  # over the capture: both refuse
    assert conf.trace.refused == 1
    conf.advance(400)
    assert conf.s["res"] != dm.NONE


# --- FX: decimal semantics over integers ----------------------------------------------
DAY0 = date(2026, 1, 1).toordinal()


@given(
    amount=st.integers(min_value=0, max_value=10**13),
    src=st.sampled_from(CURRENCIES),
    dst=st.sampled_from(CURRENCIES),
    day=st.integers(min_value=0, max_value=730),
    seed=st.integers(min_value=0, max_value=3),
)
def test_integer_fx_model_matches_decimal_fx(
    amount: int, src: str, dst: str, day: int, seed: int
) -> None:
    table = FxTable(seed)
    want = table.convert(amount, src, dst, date.fromordinal(DAY0 + day))
    assert FxModel(table).convert(amount, src, dst, DAY0 + day) == want


def _ticks() -> Iterator[tuple[int, int]]:
    for n in range(-7, 8):
        yield n, 2


@pytest.mark.parametrize(("n", "d"), list(_ticks()))
def test_round_half_even_on_halves(n: int, d: int) -> None:
    from decimal import ROUND_HALF_EVEN, Decimal
    from fractions import Fraction

    want = int((Decimal(n) / Decimal(d)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    assert round_half_even(Fraction(n, d)) == want


def test_round_sig_is_a_28_digit_decimal_context() -> None:
    from decimal import ROUND_HALF_EVEN, Context, Decimal
    from fractions import Fraction

    ctx = Context(prec=28, rounding=ROUND_HALF_EVEN)
    for n, d in [(1, 3), (2, 3), (10**30 + 5, 10), (123456789, 7), (-5, 9), (1, 10**40)]:
        assert round_sig(Fraction(n, d)) == Fraction(ctx.divide(Decimal(n), Decimal(d)))
