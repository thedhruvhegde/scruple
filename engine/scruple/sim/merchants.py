"""Synthetic merchant profiles: MCC, monthly volume and chargeback history.

All values are synthetic (docs/ASSUMPTIONS.md A-18). Monthly history feeds the
monitoring programmes in `scruple.sim.monitoring`. Ratios use `Decimal` so that
threshold comparisons are exact.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# Merchant category codes used by the original simulator (scruple.sim.generator.MCCS).
MCCS: tuple[str, ...] = ("5732", "5812", "5999", "4722", "5651")


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Synthetic chargeback-rate ranges per tier, in basis points of settled transactions.
TIER_BPS: dict[RiskTier, tuple[int, int]] = {
    RiskTier.LOW: (10, 60),
    RiskTier.MEDIUM: (60, 140),
    RiskTier.HIGH: (140, 400),
}
TIER_WEIGHTS: dict[RiskTier, float] = {RiskTier.LOW: 0.6, RiskTier.MEDIUM: 0.3, RiskTier.HIGH: 0.1}


class MonthStats(BaseModel):
    model_config = _FROZEN

    month: date  # first day of the month
    settled_txns: int = Field(ge=0)
    chargebacks: int = Field(ge=0)
    fraud_reports: int = Field(ge=0)  # issuer-reported fraud (TC40-like, S13)

    @model_validator(mode="after")
    def _first_of_month(self) -> MonthStats:
        if self.month.day != 1:
            raise ValueError("month must be the first day of a month")
        if self.chargebacks > self.settled_txns:
            raise ValueError("more chargebacks than settled transactions")
        return self


class MerchantProfile(BaseModel):
    model_config = _FROZEN

    merchant_id: str
    mcc: str = Field(pattern=r"^\d{4}$")
    tier: RiskTier
    history: tuple[MonthStats, ...]

    @model_validator(mode="after")
    def _chronological(self) -> MerchantProfile:
        months = [m.month for m in self.history]
        if months != sorted(months) or len(set(months)) != len(months):
            raise ValueError("history must be strictly chronological")
        return self

    def month(self, month: date) -> MonthStats | None:
        return next((m for m in self.history if m.month == month), None)

    def chargeback_ratio(self, month: date | None = None) -> Decimal:
        """Chargebacks / settled transactions for a month (latest by default)."""
        m = self.history[-1] if month is None else self.month(month)
        if m is None or m.settled_txns == 0:
            return Decimal(0)
        return Decimal(m.chargebacks) / Decimal(m.settled_txns)

    @property
    def total_chargebacks(self) -> int:
        return sum(m.chargebacks for m in self.history)


def _add_months(d: date, k: int) -> date:
    y, m = divmod(d.month - 1 + k, 12)
    return date(d.year + y, m + 1, 1)


def generate_merchants(
    seed: int, n: int = 40, months: int = 6, end_month: date = date(2026, 1, 1)
) -> tuple[MerchantProfile, ...]:
    """Seeded merchants `m-001` .. `m-<n>` with `months` of history ending at end_month."""
    rng = random.Random(f"merchants:{seed}")
    tiers = sorted(TIER_WEIGHTS, key=lambda t: t.value)
    out: list[MerchantProfile] = []
    for i in range(1, n + 1):
        tier = rng.choices(tiers, weights=[TIER_WEIGHTS[t] for t in tiers], k=1)[0]
        lo, hi = TIER_BPS[tier]
        base = int(rng.lognormvariate(8.5, 1.2)) + 200  # settled txns per month
        hist: list[MonthStats] = []
        for k in range(months):
            settled = max(1, int(base * rng.uniform(0.8, 1.2)))
            cbs = settled * rng.randint(lo, hi) // 10_000
            fraud = cbs * rng.randint(20, 60) // 100
            hist.append(
                MonthStats(
                    month=_add_months(end_month, k - months + 1),
                    settled_txns=settled,
                    chargebacks=cbs,
                    fraud_reports=fraud,
                )
            )
        out.append(
            MerchantProfile(
                merchant_id=f"m-{i:03d}", mcc=rng.choice(MCCS), tier=tier, history=tuple(hist)
            )
        )
    return tuple(out)
