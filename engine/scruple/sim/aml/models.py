"""AML alert triage cases: a synthetic transaction graph around one alerted account.

All data is synthetic: accounts, names, timestamps and amounts come from a seed. A
case is one transaction-monitoring alert on a subject account at our (synthetic)
bank, the subject's KYC profile, its counterparties and every transfer in a 60-day
lookback. `AmlCase.view()` is what the analyst sees first; the rest is read through
tools. `AmlCase.truth` (the family, whether it is a real typology, which transfers
make it up) and `AmlCase.rfi` (the reply a request for information would get) never
leave the environment until asked for.

The one real-world rule used is the currency transaction report threshold: a bank
files a CTR for each transaction in currency of more than $10,000 (31 CFR 1010.311;
docs/WORKFLOWS.md M1). Every other parameter is an ASSUMPTION (AM-*).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CTR_THRESHOLD_MINOR = 1_000_000  # 31 CFR 1010.311: currency transactions of more than $10,000
CASH = "CASH"  # the pseudo-account on the other side of a cash deposit or withdrawal
LOOKBACK_DAYS = 60
WINDOW_DAYS = 14


class AmlFamily(StrEnum):
    # typologies (suspicious: the truth expects a SAR)
    STRUCTURING = "structuring"  # sub-threshold cash deposits that avoid a CTR
    MULE_RING = "mule_ring"  # fan-in from, or fan-out to, new accounts, passed straight on
    RAPID_MOVEMENT = "rapid_movement"  # large wires in and out within hours, repeatedly
    # benign lookalikes (false positives: the truth expects the alert closed)
    CASH_BUSINESS = "cash_business"  # a cash-intensive business banking its takings
    MARKETPLACE_SELLER = "marketplace_seller"  # many small buyer payments, money stays
    PAYROLL = "payroll"  # a client payment, then a pay run to long-standing staff
    PROPERTY_PURCHASE = "property_purchase"  # sale proceeds in, one wire to a title company


FAMILIES: tuple[AmlFamily, ...] = tuple(AmlFamily)
TYPOLOGIES = frozenset({AmlFamily.STRUCTURING, AmlFamily.MULE_RING, AmlFamily.RAPID_MOVEMENT})
BENIGN = frozenset(FAMILIES) - TYPOLOGIES


class AlertRule(StrEnum):
    """What the (synthetic) monitoring system flagged. Each rule fires on a typology
    and on its benign lookalike, so the rule never gives the answer away."""

    CASH_NEAR_THRESHOLD = "cash_near_threshold"  # structuring, cash_business
    FAN_IN = "fan_in"  # mule_ring (collector), marketplace_seller
    FAN_OUT = "fan_out"  # mule_ring (distributor), payroll
    PASS_THROUGH = "pass_through"  # noqa: S105 - rapid_movement, property_purchase


Decision = Literal["close", "escalate", "file_sar"]
Channel = Literal["cash", "wire", "ach", "p2p", "card"]


class Account(BaseModel):
    model_config = _FROZEN

    account_id: str
    internal: bool  # held at our bank (KYC on file) or at another bank
    holder: str
    kind: Literal["individual", "business"]
    bank: str
    country: str = "US"  # synthetic
    opened_on: date | None = None  # internal accounts only
    occupation: str = ""  # KYC: occupation or line of business
    cash_intensive: bool = False
    expected_monthly_in_minor: int | None = Field(default=None, ge=0)  # KYC declaration

    def view(self) -> dict[str, object]:
        out = self.model_dump(mode="json")
        if not self.internal:  # another bank's customer: we know only what the wire said
            for k in ("opened_on", "occupation", "cash_intensive", "expected_monthly_in_minor"):
                out.pop(k)
        out["synthetic"] = True
        return out


class Transfer(BaseModel):
    model_config = _FROZEN

    txn_id: str
    at: datetime  # synthetic bank-local time
    src: str  # account id, or CASH for a cash deposit
    dst: str  # account id, or CASH for a cash withdrawal
    amount_minor: int = Field(gt=0)
    currency: Literal["USD"] = "USD"
    channel: Channel
    branch: str = ""  # cash only
    memo: str = Field(default="", max_length=60)

    @model_validator(mode="after")
    def _sides(self) -> Transfer:
        if self.src == self.dst:
            raise ValueError("a transfer needs two different sides")
        if (CASH in (self.src, self.dst)) != (self.channel == "cash"):
            raise ValueError("cash transfers, and only they, touch the CASH account")
        return self


class Alert(BaseModel):
    model_config = _FROZEN

    alert_id: str
    subject_id: str
    rule: AlertRule
    raised_on: date
    window_start: date
    window_end: date
    linked_txn_ids: tuple[str, ...] = Field(min_length=1)

    def view(self) -> dict[str, object]:
        out = self.model_dump(mode="json")
        out["synthetic"] = True
        return out


class RfiResponse(BaseModel):
    """What a request for information to the relationship manager brings back."""

    model_config = _FROZEN

    responded: bool
    explanation: str = ""
    documents: tuple[str, ...] = ()


class AmlTruth(BaseModel):
    model_config = _FROZEN

    family: AmlFamily
    suspicious: bool
    expected: Decision  # file_sar for a typology, close for a lookalike
    typology_txn_ids: tuple[str, ...] = ()  # the transfers that make up the typology


class AmlCase(BaseModel):
    model_config = _FROZEN

    case_id: str
    subject_id: str
    accounts: tuple[Account, ...]
    transfers: tuple[Transfer, ...]
    alert: Alert
    opening_balance_minor: int = Field(ge=0)  # the subject's balance at the lookback start
    lookback_start: date
    difficulty: float = Field(ge=0, le=1)
    rfi: RfiResponse
    truth: AmlTruth

    @model_validator(mode="after")
    def _consistent(self) -> AmlCase:
        ids = {a.account_id for a in self.accounts}
        if self.subject_id not in ids or self.alert.subject_id != self.subject_id:
            raise ValueError("the subject must be an account of the case")
        txns = {t.txn_id for t in self.transfers}
        if len(txns) != len(self.transfers):
            raise ValueError("duplicate transfer id")
        for t in self.transfers:
            for side in (t.src, t.dst):
                if side != CASH and side not in ids:
                    raise ValueError(f"{t.txn_id} names an unknown account {side}")
        if not set(self.alert.linked_txn_ids) <= txns:
            raise ValueError("linked transfers must exist")
        if not set(self.truth.typology_txn_ids) <= txns:
            raise ValueError("typology transfers must exist")
        if self.truth.suspicious != (self.truth.family in TYPOLOGIES):
            raise ValueError("suspicious must match the family")
        if [t.at for t in self.transfers] != sorted(t.at for t in self.transfers):
            raise ValueError("transfers must be in time order")
        return self

    @property
    def subject(self) -> Account:
        return self.account(self.subject_id)

    def account(self, account_id: str) -> Account:
        for a in self.accounts:
            if a.account_id == account_id:
                return a
        raise KeyError(account_id)

    def transfer(self, txn_id: str) -> Transfer:
        for t in self.transfers:
            if t.txn_id == txn_id:
                return t
        raise KeyError(txn_id)

    def transfers_of(self, account_id: str) -> tuple[Transfer, ...]:
        return tuple(t for t in self.transfers if account_id in (t.src, t.dst))

    def subject_balances(self) -> tuple[int, ...]:
        """The subject's running balance after each of its transfers, in order."""
        bal, out = self.opening_balance_minor, []
        for t in self.transfers_of(self.subject_id):
            bal += t.amount_minor if t.dst == self.subject_id else -t.amount_minor
            out.append(bal)
        return tuple(out)

    def view(self) -> dict[str, object]:
        """The alert as it lands in the analyst's queue (no truth, no RFI reply)."""
        return {
            "case_id": self.case_id,
            "alert": self.alert.view(),
            "subject_id": self.subject_id,
            "lookback_start": self.lookback_start.isoformat(),
            "synthetic": True,
        }


__all__ = [
    "BENIGN",
    "CASH",
    "CTR_THRESHOLD_MINOR",
    "FAMILIES",
    "LOOKBACK_DAYS",
    "TYPOLOGIES",
    "WINDOW_DAYS",
    "Account",
    "Alert",
    "AlertRule",
    "AmlCase",
    "AmlFamily",
    "AmlTruth",
    "Channel",
    "Decision",
    "RfiResponse",
    "Transfer",
]
