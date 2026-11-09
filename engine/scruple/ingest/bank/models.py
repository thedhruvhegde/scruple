"""Bank statement records shared by the BAI2, camt.053 and CSV parsers.

Amounts are integer minor units. `StatementEntry.amount_minor` is unsigned and the
direction says which way the money moved; `signed_minor` is positive for credits.
`description` is the bank's free text: it is passed through
`scruple.security.redact.redact_text` on construction, so an entry never holds raw
emails, card numbers, phone numbers or cued names, then truncated.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scruple.ingest.currency import Currency
from scruple.security.redact import redact_text


class Direction(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class StatementBalance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(max_length=35)  # BAI2 status code ("010") or ISO code ("OPBD")
    amount_minor: int  # signed: negative is a debit balance
    as_of: date | None = None


class StatementEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_minor: int = Field(ge=0)
    direction: Direction | None  # None only for codes with no direction (BAI2 misc)
    booking_date: date | None = None
    value_date: date | None = None
    type_code: str | None = Field(default=None, max_length=64)
    bank_ref: str | None = Field(default=None, max_length=64)
    customer_ref: str | None = Field(default=None, max_length=64)
    end_to_end_id: str | None = Field(default=None, max_length=64)
    return_reason: str | None = Field(default=None, max_length=35)
    status: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("description", mode="before")
    @classmethod
    def _redact(cls, v: object) -> object:
        if isinstance(v, str):
            return truncate(redact_text(v[:20_000]).text, 500)
        return v

    @property
    def signed_minor(self) -> int:
        if self.direction is Direction.DEBIT:
            return -self.amount_minor
        return self.amount_minor if self.direction is Direction.CREDIT else 0


class BankStatement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: str = Field(pattern=r"^(bai2|camt053|csv)$")
    statement_id: str | None = Field(default=None, max_length=64)
    account_id: str = Field(min_length=1, max_length=64)
    currency: Currency
    balances: tuple[StatementBalance, ...] = ()
    entries: tuple[StatementEntry, ...] = ()

    def net_minor(self) -> int:
        return sum(e.signed_minor for e in self.entries)


def truncate(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    s = " ".join(text.split())
    return s[:n] or None
