"""Bank reconciliation cases: the internal cash book, the bank statement, the hidden breaks.

All data is synthetic. A case is one bank account over one statement period. The
internal ledger (the cash book) lists what the company booked; the bank statement is
what the bank booked, delivered as a camt.053 or BAI2 file and read back through the
real parsers (`scruple.ingest.bank`). Where the two disagree there is a break. The
truth lists every break and how a correct reconciler treats it; it never leaves the
environment.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class BreakKind(StrEnum):
    TIMING = "timing"  # booked at period end, on the bank's next statement: outstanding
    DUPLICATE_LEDGER = "duplicate_ledger"  # booked twice internally: reverse one
    DUPLICATE_BANK = "duplicate_bank"  # the bank posted twice: escalate the copy
    FX = "fx"  # booked at one rate, settled at another: fx adjustment
    FEE_NET = "fee_net"  # a settlement arrived net of fees: fee adjustment
    FEE_LINE = "fee_line"  # a bank charge with no book entry: fee adjustment
    MISSING_IDENTIFIED = "missing_identified"  # unbooked receipt for an open invoice: book it
    MISSING_UNIDENTIFIED = "missing_unidentified"  # unbooked receipt, no reference: escalate
    MISSING_BANK = "missing_bank"  # booked mid-period, never reached the bank: escalate
    PARTIAL_SPLIT = "partial_split"  # one booking settled in two bank lines: match together
    PARTIAL_SHORT = "partial_short"  # short payment: reinstate the rest as receivable


BREAK_KINDS: tuple[BreakKind, ...] = tuple(BreakKind)

AdjustKind = Literal["fee", "fx", "missing", "partial", "duplicate_reversal"]
ADJUST_KINDS: tuple[str, ...] = ("fee", "fx", "missing", "partial", "duplicate_reversal")
LineKind = Literal["customer_receipt", "processor_settlement", "vendor_payment", "payroll"]


class LedgerLine(BaseModel):
    """One cash-book line. `amount_minor` is signed: positive is money in."""

    model_config = _FROZEN

    line_id: str
    booked_on: date
    amount_minor: int
    reference: str = Field(max_length=35)
    kind: LineKind
    offset_account: str  # the other side of the booking (ar:<invoice>, ap:<vendor>, ...)
    description: str = Field(max_length=140)
    invoice_id: str | None = None
    original_currency: str | None = None  # an invoice in another currency
    original_amount_minor: int | None = None


class Invoice(BaseModel):
    """An open receivable the company is waiting to be paid (observable)."""

    model_config = _FROZEN

    invoice_id: str
    amount_minor: int = Field(gt=0)
    currency: str
    customer: str


class BankLine(BaseModel):
    """What the bank booked, before it is written to a statement file."""

    model_config = _FROZEN

    line_id: str  # S-001.. in statement order
    booking_date: date
    amount_minor: int  # signed: positive is a credit to the account
    reference: str = Field(max_length=35)
    bank_ref: str = Field(max_length=35)
    type_code: str
    description: str = Field(max_length=140)


class Adjustment(BaseModel):
    """An adjusting entry a correct reconciler posts: cash +amount, offset -amount."""

    model_config = _FROZEN

    kind: AdjustKind
    amount_minor: int
    offset_account: str
    statement_line_ids: tuple[str, ...]
    ledger_line_ids: tuple[str, ...] = ()


class Treatment(BaseModel):
    model_config = _FROZEN

    match: tuple[tuple[str, ...], tuple[str, ...]] | None = None  # (statement ids, ledger ids)
    adjustments: tuple[Adjustment, ...] = ()
    timing: tuple[str, ...] = ()  # ledger ids left outstanding
    escalate_statement: tuple[str, ...] = ()
    escalate_ledger: tuple[str, ...] = ()


class Break(BaseModel):
    model_config = _FROZEN

    break_id: str
    kind: BreakKind
    statement_line_ids: tuple[str, ...]
    ledger_line_ids: tuple[str, ...]
    expected: Treatment
    alternatives: tuple[Treatment, ...] = ()  # equally correct treatments

    @property
    def treatments(self) -> tuple[Treatment, ...]:
        return (self.expected, *self.alternatives)


class ReconTruth(BaseModel):
    model_config = _FROZEN

    breaks: tuple[Break, ...]
    clean: tuple[tuple[str, str], ...]  # (statement id, ledger id) pairs with no break


class ReconCase(BaseModel):
    model_config = _FROZEN

    case_id: str
    account_id: str
    currency: Literal["USD", "EUR"]
    period_start: date
    period_end: date
    opening_balance_minor: int  # bank and book agreed at the start (reconciled before)
    statement_format: Literal["camt053", "bai2"]
    statement_file: str  # the file as the bank delivered it
    bank_lines: tuple[BankLine, ...]  # generator view; the agent sees the parsed file
    ledger_lines: tuple[LedgerLine, ...]
    invoices: tuple[Invoice, ...]
    difficulty: float = Field(ge=0, le=1)
    truth: ReconTruth

    @property
    def closing_balance_minor(self) -> int:
        return self.opening_balance_minor + sum(b.amount_minor for b in self.bank_lines)

    def view(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "account_id": self.account_id,
            "currency": self.currency,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "book_opening_balance_minor": self.opening_balance_minor,
            "statement_format": self.statement_format,
            "statement_lines": len(self.bank_lines),
            "ledger_lines": len(self.ledger_lines),
            "open_invoices": len(self.invoices),
            "synthetic": True,
        }


__all__ = [
    "ADJUST_KINDS",
    "BREAK_KINDS",
    "AdjustKind",
    "Adjustment",
    "BankLine",
    "Break",
    "BreakKind",
    "Invoice",
    "LedgerLine",
    "LineKind",
    "ReconCase",
    "ReconTruth",
    "Treatment",
]
