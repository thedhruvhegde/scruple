"""Merchant monitoring programmes: Visa VAMP and Mastercard ECM / HECM.

Thresholds and fees as publicly reported (docs/SOURCES.md); everything not sourced is
an ASSUMPTION (docs/ASSUMPTIONS.md A-29). Visa retired VDMP and VFMP on 31 Mar 2025
and folded them into VAMP from 1 Apr 2025 (S12), so only VAMP is modelled for Visa.

Visa VAMP, merchant level (S13):
  ratio = (fraud reports + disputes) / settled transactions, per month
  "excessive" when the monthly fraud + dispute count is at least 1,500 and the ratio is
  at least 2.2% (from 1 Jun 2025) or 1.5% (from 1 Apr 2026)
  fee: $8 per dispute from 1 Oct 2025 (one secondary source)

Mastercard ECM / HECM (S14, S15):
  ECM when a month has at least 100 chargebacks and a ratio of at least 1.5%;
  HECM at 300 and 3%. Exit after three consecutive months below the ECM thresholds.
  Fines by month in the programme: 0, $1,000, then $1,000 (ECM) or $2,000 (HECM).
  ASSUMPTIONS: the ratio uses the previous month's settled transactions (the current
  month's when there is none); fines after month 3 stay at the month-3 level.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from scruple.sim.card_ledger import CardLedger
from scruple.sim.merchants import MerchantProfile, MonthStats

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Program(StrEnum):
    VAMP = "visa_vamp"
    ECM = "mastercard_ecm"


class Level(StrEnum):
    NONE = "none"
    EXCESSIVE = "excessive"  # VAMP
    ECM = "ecm"
    HECM = "hecm"


class Rule(BaseModel):
    """One dated parameter, with where it comes from."""

    model_config = _FROZEN

    effective: date
    value: Decimal
    source: str | None  # docs/SOURCES.md id; None means ASSUMPTION
    note: str = ""


VAMP_MIN_COUNT = Rule(effective=date(2025, 4, 1), value=Decimal(1_500), source="S13")
VAMP_RATIO: tuple[Rule, ...] = (
    Rule(effective=date(2025, 6, 1), value=Decimal("0.022"), source="S13"),
    Rule(effective=date(2026, 4, 1), value=Decimal("0.015"), source="S13"),
)
VAMP_FEE_PER_DISPUTE_USD: tuple[Rule, ...] = (
    Rule(effective=date(2025, 10, 1), value=Decimal(800), source="S13", note="one source"),
)

ECM_MIN = (Rule(effective=date(2019, 10, 1), value=Decimal(100), source="S14"),)
ECM_RATIO = (Rule(effective=date(2019, 10, 1), value=Decimal("0.015"), source="S14"),)
HECM_MIN = (Rule(effective=date(2019, 10, 1), value=Decimal(300), source="S14"),)
HECM_RATIO = (Rule(effective=date(2019, 10, 1), value=Decimal("0.03"), source="S14"),)
ECM_EXIT_MONTHS = 3  # S15
# fines in USD minor units by month in programme (index 0 = first month), S14
ECM_FINES = (0, 100_000, 100_000)
HECM_FINES = (0, 100_000, 200_000)


def in_force(rules: tuple[Rule, ...], month: date) -> Rule | None:
    """The latest rule effective on or before `month`, if any."""
    live = [r for r in rules if r.effective <= month]
    return max(live, key=lambda r: r.effective) if live else None


class MonthStatus(BaseModel):
    model_config = _FROZEN

    merchant_id: str
    program: Program
    month: date
    count: int  # VAMP: fraud + disputes; ECM: chargebacks
    ratio: Decimal
    level: Level
    month_in_program: int  # 0 when not in the programme
    fine_usd_minor: int


def evaluate_vamp(profile: MerchantProfile) -> tuple[MonthStatus, ...]:
    out: list[MonthStatus] = []
    months_in = 0
    for m in profile.history:
        count = m.fraud_reports + m.chargebacks
        ratio = Decimal(count) / Decimal(m.settled_txns) if m.settled_txns else Decimal(0)
        threshold = in_force(VAMP_RATIO, m.month)
        level = Level.NONE
        if (
            threshold is not None
            and m.month >= VAMP_MIN_COUNT.effective
            and count >= VAMP_MIN_COUNT.value
            and ratio >= threshold.value
        ):
            level = Level.EXCESSIVE
        months_in = months_in + 1 if level is Level.EXCESSIVE else 0
        fee_rule = in_force(VAMP_FEE_PER_DISPUTE_USD, m.month)
        fine = 0
        if level is Level.EXCESSIVE and fee_rule is not None:
            fine = int(fee_rule.value) * m.chargebacks
        out.append(
            MonthStatus(
                merchant_id=profile.merchant_id,
                program=Program.VAMP,
                month=m.month,
                count=count,
                ratio=ratio,
                level=level,
                month_in_program=months_in,
                fine_usd_minor=fine,
            )
        )
    return tuple(out)


def _ecm_ratio(prev: MonthStats | None, m: MonthStats) -> Decimal:
    denom = prev.settled_txns if prev is not None and prev.settled_txns else m.settled_txns
    return Decimal(m.chargebacks) / Decimal(denom) if denom else Decimal(0)


def _rule(rules: tuple[Rule, ...], month: date) -> Decimal | None:
    r = in_force(rules, month)
    return None if r is None else r.value


def evaluate_ecm(profile: MerchantProfile) -> tuple[MonthStatus, ...]:
    out: list[MonthStatus] = []
    enrolled, months_in, below = False, 0, 0
    prev: MonthStats | None = None
    for m in profile.history:
        ratio = _ecm_ratio(prev, m)
        e_min, e_ratio = _rule(ECM_MIN, m.month), _rule(ECM_RATIO, m.month)
        h_min, h_ratio = _rule(HECM_MIN, m.month), _rule(HECM_RATIO, m.month)
        level = Level.NONE
        if (
            h_min is not None
            and h_ratio is not None
            and m.chargebacks >= h_min
            and ratio >= h_ratio
        ):
            level = Level.HECM
        elif (
            e_min is not None
            and e_ratio is not None
            and m.chargebacks >= e_min
            and ratio >= e_ratio
        ):
            level = Level.ECM
        fine = 0
        if level is not Level.NONE:
            enrolled, below = True, 0
            months_in += 1
            table = HECM_FINES if level is Level.HECM else ECM_FINES
            fine = table[min(months_in, len(table)) - 1]
        elif enrolled:
            below += 1
            if below >= ECM_EXIT_MONTHS:
                enrolled, months_in, below = False, 0, 0
        out.append(
            MonthStatus(
                merchant_id=profile.merchant_id,
                program=Program.ECM,
                month=m.month,
                count=m.chargebacks,
                ratio=ratio,
                level=level,
                month_in_program=months_in if enrolled else 0,
                fine_usd_minor=fine,
            )
        )
        prev = m
    return tuple(out)


def evaluate(profile: MerchantProfile) -> tuple[MonthStatus, ...]:
    return (*evaluate_vamp(profile), *evaluate_ecm(profile))


def post_fines(ledger: CardLedger, statuses: tuple[MonthStatus, ...]) -> int:
    """Post every non-zero fine to the ledger. Returns the number of entries posted."""
    n = 0
    for s in statuses:
        if s.fine_usd_minor > 0:
            ledger.merchant_charge(
                f"fine:{s.program.value}",
                s.merchant_id,
                s.fine_usd_minor,
                "network:fines",
                ref=f"{s.program.value}:{s.merchant_id}:{s.month.isoformat()}",
            )
            n += 1
    return n
