"""Multi-currency amounts with FX at settlement. Decimal everywhere, explicit rounding.

Amounts are integers in each currency's minor unit. Minor-unit exponents follow ISO
4217 (docs/SOURCES.md S17). Exchange rates are SYNTHETIC (docs/ASSUMPTIONS.md A-27):
a fixed base rate per currency against USD, moved by a deterministic daily drift of
at most +/-3% derived from (seed, currency, date). They are not market rates.

Rounding: every conversion goes through `Decimal` with a 28-digit context and is
rounded once, to the target minor unit, with ROUND_HALF_EVEN (banker's rounding).
Rates are quoted to 8 decimal places, also ROUND_HALF_EVEN. A round trip A -> B -> A
on the same day is off by at most `round_trip_bound(A, B, day)` minor units of A.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext

from scruple.hashing import sha256_hex

EXPONENT: dict[str, int] = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}
CURRENCIES: tuple[str, ...] = tuple(EXPONENT)

# Synthetic base rates: USD per one major unit of the currency (A-27).
BASE_USD_PER_UNIT: dict[str, Decimal] = {
    "USD": Decimal(1),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "JPY": Decimal("0.0067"),
}
MAX_DRIFT = Decimal("0.03")
RATE_PLACES = Decimal("0.00000001")
_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


class FxTable:
    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def usd_per_unit(self, currency: str, day: date) -> Decimal:
        """Synthetic USD value of one major unit of `currency` on `day`."""
        base = BASE_USD_PER_UNIT[currency]
        if currency == "USD":
            return base
        h = int(sha256_hex(f"fx:{self.seed}:{currency}:{day.isoformat()}")[:8], 16)
        drift = (Decimal(h) / Decimal(0xFFFFFFFF) * 2 - 1) * MAX_DRIFT  # in [-3%, +3%]
        with localcontext(_CTX):
            return (base * (1 + drift)).quantize(RATE_PLACES, rounding=ROUND_HALF_EVEN)

    def rate(self, src: str, dst: str, day: date) -> Decimal:
        """Major units of `dst` per major unit of `src` on `day`."""
        if src == dst:
            return Decimal(1)
        with localcontext(_CTX):
            return self.usd_per_unit(src, day) / self.usd_per_unit(dst, day)

    def minor_factor(self, src: str, dst: str, day: date) -> Decimal:
        """Minor units of `dst` per minor unit of `src`."""
        with localcontext(_CTX):
            return self.rate(src, dst, day).scaleb(EXPONENT[dst] - EXPONENT[src])

    def convert(self, amount_minor: int, src: str, dst: str, day: date) -> int:
        if src == dst:
            return amount_minor
        with localcontext(_CTX):
            raw = Decimal(amount_minor) * self.minor_factor(src, dst, day)
            return int(raw.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))

    def round_trip_bound(self, src: str, dst: str, day: date) -> int:
        """Max |x - convert(convert(x, src, dst), dst, src)| in minor units of `src`.

        y = round(x*k) is within 0.5 of x*k, so y/k is within 0.5/k of x, and rounding
        adds at most 0.5 more. The error is an integer, so the bound is
        floor(0.5 + 0.5/k) for k = minor_factor (plus 1e-9 slack for the 28-digit
        inverse rate)."""
        if src == dst:
            return 0
        with localcontext(_CTX):
            k = self.minor_factor(src, dst, day)
            b = Decimal("0.5") + Decimal("0.5") / k + Decimal("1e-9")
            return int(b.to_integral_value(rounding=ROUND_FLOOR))
