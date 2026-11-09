"""Simulator data model. Everything here is synthetic.

A `DisputeCase` carries both observable features (what a rule may see) and hidden
ground truth (`scenario`, `claim_valid`). Only `CaseView` is ever handed to a rule.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from scruple.domain import Action, ReasonCode

# Synthetic FX: USD minor units per 100 minor units of the currency (fixed table).
# Assumption: real FX handling is out of scope; see docs/ASSUMPTIONS.md.
FX_TO_USD_PER_100: dict[str, int] = {"USD": 100, "EUR": 108, "GBP": 127}

# Synthetic fees and costs in USD minor units. Not real network fees.
REPRESENTMENT_LOSS_FEE_USD = 1_500  # $15: representment rejected (e.g. late evidence)
ARBITRATION_FEE_USD = 25_000  # $250: contested a valid claim, lost in arbitration
ESCALATION_COST_USD = 3_500  # $35: analyst time to resolve by hand
HIGH_VALUE_THRESHOLD_USD = 1_000_000  # $10,000


def to_usd(amount_minor: int, currency: str) -> int:
    return amount_minor * FX_TO_USD_PER_100[currency] // 100


def from_usd(amount_usd_minor: int, currency: str) -> int:
    return amount_usd_minor * 100 // FX_TO_USD_PER_100[currency]


class Scenario(StrEnum):
    """Hidden slice label used for per-slice scoring. Rules can never see it."""

    FRAUD = "fraud"
    NOT_RECEIVED = "not_received"
    NOT_AS_DESCRIBED = "not_as_described"
    PARTIAL_REFUND = "partial_refund"
    LATE_EVIDENCE = "late_evidence"
    DUPLICATE_DISPUTE = "duplicate_dispute"
    CURRENCY_MISMATCH = "currency_mismatch"
    HIGH_VALUE = "high_value"


class Stage(StrEnum):
    INQUIRY = "inquiry"
    CHARGEBACK = "chargeback"
    REPRESENTMENT = "representment"
    ARBITRATION = "arbitration"
    RESOLVED = "resolved"


class Resolution(StrEnum):
    ACCEPTED = "accepted"
    WON = "won"
    PARTIAL = "partial"
    LOST = "lost"


class DisputeCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    txn_id: str
    merchant_id: str
    mcc: str
    reason_code: ReasonCode
    amount_minor: int = Field(gt=0)  # chargeback amount, dispute currency
    dispute_currency: str
    txn_currency: str
    captured_minor: int = Field(gt=0)  # original capture, txn currency
    refunded_minor: int = Field(ge=0)  # prior refunds, txn currency
    delivery_confirmed: bool
    three_ds: bool
    evidence_ready_day: int
    response_deadline_day: int
    prior_dispute_on_txn: bool
    # --- hidden ground truth -------------------------------------------------
    scenario: Scenario
    claim_valid: bool

    @property
    def chargeback_txn_minor(self) -> int:
        """Chargeback amount converted into the transaction currency."""
        if self.dispute_currency == self.txn_currency:
            return self.amount_minor
        return from_usd(to_usd(self.amount_minor, self.dispute_currency), self.txn_currency)

    @property
    def liability_txn_minor(self) -> int:
        """What the merchant truly owes the cardholder for this dispute."""
        if not self.claim_valid:
            return 0
        return min(self.chargeback_txn_minor, self.captured_minor - self.refunded_minor)

    def view(self) -> dict[str, bool | int | str]:
        """The observable features. Must match scruple.domain.FIELD_TYPES exactly."""
        return {
            "mcc": self.mcc,
            "reason_code": self.reason_code.value,
            "amount_usd_minor": to_usd(self.amount_minor, self.dispute_currency),
            "delivery_confirmed": self.delivery_confirmed,
            "three_ds": self.three_ds,
            "evidence_on_time": self.evidence_ready_day <= self.response_deadline_day,
            "prior_dispute_on_txn": self.prior_dispute_on_txn,
            "has_prior_refund": self.refunded_minor > 0,
            "currency_mismatch": self.dispute_currency != self.txn_currency,
        }


class CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    txn_id: str
    scenario: Scenario
    action: Action
    decided_by: str  # rule ref such as "inr-contest@v2", or "default"
    stages: tuple[Stage, ...]
    resolution: Resolution
    debit_txn_minor: int
    fee_usd_minor: int
    ops_usd_minor: int
    cost_usd_minor: int
    oracle_cost_usd_minor: int
    regret_usd_minor: int
    amount_usd_minor: int
