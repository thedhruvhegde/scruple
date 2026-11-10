"""ACH return cases: what the originator sees, and the hidden truth verifiers score against.

All data is synthetic. A case is one originated debit entry (the originator collects
an invoice from a receiver's bank account) that came back as a return. The agent works
the return on behalf of the originator; `AchCase.view()` is everything it may see.
`AchCase.truth` (the family, when the receiver's funds arrive, and the allocation a
correct handling ends with) never leaves the environment.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scruple.sim.ach.rules import CODES, CONSUMER_SEC, CORPORATE_SEC

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class AchFamily(StrEnum):
    NSF_RECOVERABLE = "nsf_recoverable"  # R01/R09; funds arrive later: reinitiate
    NSF_INSOLVENT = "nsf_insolvent"  # R01/R09; funds never arrive: write off
    ACCOUNT_CLOSED = "account_closed"  # R02/R03/R04/R16/R20: write off
    UNAUTHORIZED = "unauthorized"  # R07/R10/R11 (consumer) or R29 (corporate): accept
    R05_EXTENDED = "r05_extended"  # R05 on a CCD entry, late-looking but timely: accept
    STOP_PAYMENT = "stop_payment"  # R08: accept
    UNTIMELY_RETURN = "untimely_return"  # returned after its window: dishonor R68
    DUPLICATE_RETURN = "duplicate_return"  # the RDFI returned twice: dishonor R67


FAMILIES: tuple[AchFamily, ...] = tuple(AchFamily)

Resolution = Literal["collected", "written_off", "voided"]


class Allocation(BaseModel):
    """Where the invoice amount ended up, in USD cents. The four always sum to it."""

    model_config = _FROZEN

    collected_minor: int = 0  # cash kept from the receiver
    written_off_minor: int = 0  # bad debt: owed, but not collectable through ACH
    voided_minor: int = 0  # return accepted: the debit is conceded
    open_minor: int = 0  # still receivable when the episode closed

    @property
    def total(self) -> int:
        return self.collected_minor + self.written_off_minor + self.voided_minor + self.open_minor


class ScheduledReturn(BaseModel):
    """A return the RDFI sends on its own (not caused by an agent action)."""

    model_config = _FROZEN

    return_id: str
    code: str
    settles_on: date
    trace_number: str = Field(pattern=r"^[0-9]{15}$")
    addenda_info: str = Field(default="", max_length=44)

    @model_validator(mode="after")
    def _known(self) -> ScheduledReturn:
        if self.code not in CODES:
            raise ValueError(f"unmodelled return code {self.code}")
        return self


class Customer(BaseModel):
    model_config = _FROZEN

    next_payday: date  # from the receiver's billing profile (a hint, not a promise)
    prior_returns_12m: int = Field(ge=0)


class Authorization(BaseModel):
    model_config = _FROZEN

    method: Literal["online", "written", "recorded_call"]
    obtained_on: date
    reference: str


class AchTruth(BaseModel):
    model_config = _FROZEN

    family: AchFamily
    funds_on: date | None  # first day a debit of the full amount would clear; None: never
    expected: Allocation
    resolution: Resolution


class AchCase(BaseModel):
    model_config = _FROZEN

    case_id: str
    entry_id: str
    trace_number: str = Field(pattern=r"^[0-9]{15}$")
    sec_code: Literal["PPD", "WEB", "CCD"]
    company_name: str
    company_entry_description: str = Field(max_length=10)
    amount_minor: int = Field(gt=0)
    currency: Literal["USD"] = "USD"
    invoiced_on: date
    settled_on: date  # settlement date of the original debit entry
    rdfi: str = Field(pattern=r"^[0-9]{8}$")
    account_last4: str = Field(pattern=r"^[0-9]{4}$")
    returns: tuple[ScheduledReturn, ...] = Field(min_length=1)
    customer: Customer
    authorization: Authorization
    holidays: tuple[date, ...] = ()
    difficulty: float = Field(ge=0, le=1)
    truth: AchTruth

    @model_validator(mode="after")
    def _consistent(self) -> AchCase:
        days = [r.settles_on for r in self.returns]
        if days != sorted(days) or days[0] < self.settled_on:
            raise ValueError("returns must be in date order and after settlement")
        if self.truth.expected.total != self.amount_minor:
            raise ValueError("expected allocation must sum to the amount")
        return self

    @property
    def opened_on(self) -> date:
        return self.returns[0].settles_on

    @property
    def consumer(self) -> bool:
        return self.sec_code in CONSUMER_SEC

    @property
    def corporate(self) -> bool:
        return self.sec_code in CORPORATE_SEC

    def view(self) -> dict[str, object]:
        """The observable entry (no truth, no future returns)."""
        return {
            "case_id": self.case_id,
            "entry_id": self.entry_id,
            "trace_number": self.trace_number,
            "sec_code": self.sec_code,
            "company_name": self.company_name,
            "company_entry_description": self.company_entry_description,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "invoiced_on": self.invoiced_on.isoformat(),
            "settled_on": self.settled_on.isoformat(),
            "rdfi": self.rdfi,
            "account_last4": self.account_last4,
            "customer": self.customer.model_dump(mode="json"),
            "authorization": self.authorization.model_dump(mode="json"),
            "synthetic": True,
        }


__all__ = [
    "FAMILIES",
    "AchCase",
    "AchFamily",
    "AchTruth",
    "Allocation",
    "Authorization",
    "Customer",
    "Resolution",
    "ScheduledReturn",
]
