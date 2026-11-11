"""FX conversion with `decimal` semantics, restated over integers and fractions.

`scruple.sim.fx.FxTable.convert` works in a 28-digit `Decimal` context with
ROUND_HALF_EVEN. Here the same computation is exact rational arithmetic plus two
explicit rounding functions, so every step is visible:

    rate   = round_sig(usd(src) / usd(dst), 28)          # Decimal division, 28 digits
    factor = rate * 10 ** (exp(dst) - exp(src))           # scaleb: exact
    raw    = round_sig(amount * factor, 28)               # Decimal multiplication
    result = round_half_even(raw)                         # quantize(Decimal(1))

Note the double rounding: the product is first rounded to 28 significant digits and
only then to an integer, as `decimal` does. The daily USD rates themselves are data
(synthetic, A-27) taken from `FxTable.usd_per_unit`. The conformance tests check this
model against `FxTable.convert`; `scruple.formal.smt.fx_checks` proves rounding bounds
on it for every amount up to a limit.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from scruple.sim.fx import EXPONENT, FxTable

PREC = 28


def round_half_even(q: Fraction) -> int:
    """`q` to the nearest integer, ties to even."""
    n, d = q.numerator, q.denominator
    fl = n // d
    rem = q - fl
    if rem > Fraction(1, 2) or (rem == Fraction(1, 2) and fl % 2 == 1):
        return fl + 1
    return fl


def _floor_log10(q: Fraction) -> int:
    """e with 10**e <= q < 10**(e + 1), for q > 0."""
    e = len(str(q.numerator)) - len(str(q.denominator))
    while Fraction(10) ** e > q:
        e -= 1
    while Fraction(10) ** (e + 1) <= q:
        e += 1
    return e


def round_sig(q: Fraction, prec: int = PREC) -> Fraction:
    """`q` rounded to `prec` significant digits, ties to even (a `decimal` context)."""
    if q == 0:
        return Fraction(0)
    sign = -1 if q < 0 else 1
    a = abs(q)
    e = _floor_log10(a) - (prec - 1)
    scale = Fraction(10) ** e
    return sign * round_half_even(a / scale) * scale


def as_fraction(x: Decimal) -> Fraction:
    return Fraction(x)


def minor_factor(usd_src: Fraction, usd_dst: Fraction, src: str, dst: str) -> Fraction:
    rate = round_sig(usd_src / usd_dst)
    return rate * Fraction(10) ** (EXPONENT[dst] - EXPONENT[src])


def convert(amount: int, src: str, dst: str, usd_src: Fraction, usd_dst: Fraction) -> int:
    if src == dst:
        return amount
    return round_half_even(round_sig(amount * minor_factor(usd_src, usd_dst, src, dst)))


class FxModel:
    """`convert` with the synthetic daily rates of an `FxTable` (as data)."""

    def __init__(self, table: FxTable | None = None) -> None:
        self.table = table or FxTable()

    def usd(self, currency: str, day: int) -> Fraction:
        from datetime import date

        return as_fraction(self.table.usd_per_unit(currency, date.fromordinal(day)))

    def convert(self, amount: int, src: str, dst: str, day: int) -> int:
        if src == dst:
            return amount
        return convert(amount, src, dst, self.usd(src, day), self.usd(dst, day))

    def factor(self, src: str, dst: str, day: int) -> Fraction:
        return minor_factor(self.usd(src, day), self.usd(dst, day), src, dst)
