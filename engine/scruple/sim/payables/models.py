"""Payables cases: one pay run of vendor invoices, with POs, receipts and vendor data.

All data is synthetic: vendors, numbers, dates, amounts and bank details come from a
seed. A case is a batch of invoices booked to accounts payable and waiting for a pay
run, the purchase orders and goods receipts behind them (three-way match), the vendor
master and each vendor's payment history. `PayablesCase.view()` is the batch as the
AP clerk first sees it. `truth` (what each invoice really is, and what should happen
to it) and the replies a verification would get never leave the environment.

No real-world rule is modelled: three-way match, duplicate checks and vendor
verification are standard accounts payable controls, and every parameter is an
ASSUMPTION (docs/WORKFLOWS.md AP-*).
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")

PRICE_TOLERANCE_PCT = 2  # AP-2: an invoice price up to 2% over the PO price still matches
NEW_VENDOR_DAYS = 30  # AP-4: a vendor onboarded less than 30 days before the pay run


class InvoiceKind(StrEnum):
    CLEAN = "clean"  # three-way match within tolerance: pay
    RECURRING = "recurring"  # a monthly non-PO invoice from an established vendor: pay
    PRICE_VARIANCE = "price_variance"  # price above the PO beyond tolerance: hold
    SHORT_RECEIPT = "short_receipt"  # fewer goods received than invoiced: hold
    NO_RECEIPT = "no_receipt"  # nothing received against the PO yet: hold
    DUPLICATE = "duplicate"  # the same invoice again (number written differently): reject
    PHANTOM_VENDOR = "phantom_vendor"  # a new vendor, no PO, nobody ordered it: reject
    NEW_VENDOR = "new_vendor"  # a new vendor, no PO, a real purchase: pay once verified


INVOICE_KINDS: tuple[InvoiceKind, ...] = tuple(InvoiceKind)
Disposition = Literal["paid", "held", "rejected"]
EXPECTED: dict[InvoiceKind, Disposition] = {
    InvoiceKind.CLEAN: "paid",
    InvoiceKind.RECURRING: "paid",
    InvoiceKind.NEW_VENDOR: "paid",
    InvoiceKind.PRICE_VARIANCE: "held",
    InvoiceKind.SHORT_RECEIPT: "held",
    InvoiceKind.NO_RECEIPT: "held",
    InvoiceKind.DUPLICATE: "rejected",
    InvoiceKind.PHANTOM_VENDOR: "rejected",
}


def invoice_key(number: str) -> str:
    """A vendor invoice number with formatting removed: case, separators, a leading
    "INV" and leading zeros (AP-3). "INV-01001", "inv 1001" and "1001" are one key."""
    s = re.sub(r"[^0-9A-Z]", "", number.upper())
    s = s.removeprefix("INV")
    return s.lstrip("0") or "0"


class BankAccount(BaseModel):
    model_config = _FROZEN

    routing: str = Field(pattern=r"^[0-9]{9}$")  # synthetic
    account_last4: str = Field(pattern=r"^[0-9]{4}$")

    @property
    def label(self) -> str:
        return f"{self.routing}/{self.account_last4}"


class Vendor(BaseModel):
    model_config = _FROZEN

    vendor_id: str
    name: str
    onboarded_on: date
    email_domain: str
    phone_on_file: str
    bank: BankAccount

    def view(self) -> dict[str, object]:
        out = self.model_dump(mode="json")
        out["synthetic"] = True
        return out


class PurchaseOrder(BaseModel):
    model_config = _FROZEN

    po_id: str
    vendor_id: str
    description: str
    qty: int = Field(gt=0)
    unit_price_minor: int = Field(gt=0)
    issued_on: date


class Receipt(BaseModel):
    model_config = _FROZEN

    receipt_id: str
    po_id: str
    qty: int = Field(gt=0)
    received_on: date


class Invoice(BaseModel):
    model_config = _FROZEN

    invoice_id: str  # ours
    vendor_id: str
    number: str  # the vendor's
    po_id: str | None
    description: str
    qty: int = Field(gt=0)
    unit_price_minor: int = Field(gt=0)
    issued_on: date
    due_on: date

    @property
    def amount_minor(self) -> int:
        return self.qty * self.unit_price_minor

    def view(self) -> dict[str, object]:
        out = self.model_dump(mode="json")
        out["amount_minor"] = self.amount_minor
        out["currency"] = "USD"
        return out


class PaidInvoice(BaseModel):
    """An invoice paid in an earlier pay run (payment history)."""

    model_config = _FROZEN

    vendor_id: str
    number: str
    amount_minor: int = Field(gt=0)
    paid_on: date


class InvoiceTruth(BaseModel):
    model_config = _FROZEN

    invoice_id: str
    kind: InvoiceKind
    expected: Disposition
    verification_confirms: bool = True  # what the business owner says if asked
    duplicate_of: str | None = None  # the batch invoice id it copies, or "history"


class PayablesTruth(BaseModel):
    model_config = _FROZEN

    invoices: tuple[InvoiceTruth, ...]

    def of(self, invoice_id: str) -> InvoiceTruth:
        for t in self.invoices:
            if t.invoice_id == invoice_id:
                return t
        raise KeyError(invoice_id)


class PayablesCase(BaseModel):
    model_config = _FROZEN

    case_id: str
    run_on: date  # the pay run date
    vendors: tuple[Vendor, ...]
    purchase_orders: tuple[PurchaseOrder, ...]
    receipts: tuple[Receipt, ...]
    invoices: tuple[Invoice, ...] = Field(min_length=1)
    history: tuple[PaidInvoice, ...]
    opening_cash_minor: int = Field(gt=0)
    difficulty: float = Field(ge=0, le=1)
    truth: PayablesTruth

    @model_validator(mode="after")
    def _consistent(self) -> PayablesCase:
        vids = {v.vendor_id for v in self.vendors}
        pos = {p.po_id for p in self.purchase_orders}
        if any(
            i.vendor_id not in vids or (i.po_id is not None and i.po_id not in pos)
            for i in self.invoices
        ):
            raise ValueError("invoices must name known vendors and POs")
        if {t.invoice_id for t in self.truth.invoices} != {i.invoice_id for i in self.invoices}:
            raise ValueError("every invoice needs a truth")
        if sum(i.amount_minor for i in self.invoices) > self.opening_cash_minor:
            raise ValueError("the account must cover the whole batch")
        return self

    def vendor(self, vendor_id: str) -> Vendor:
        for v in self.vendors:
            if v.vendor_id == vendor_id:
                return v
        raise KeyError(vendor_id)

    def invoice(self, invoice_id: str) -> Invoice:
        for i in self.invoices:
            if i.invoice_id == invoice_id:
                return i
        raise KeyError(invoice_id)

    def po(self, po_id: str) -> PurchaseOrder:
        for p in self.purchase_orders:
            if p.po_id == po_id:
                return p
        raise KeyError(po_id)

    def receipts_for(self, po_id: str) -> tuple[Receipt, ...]:
        return tuple(r for r in self.receipts if r.po_id == po_id)

    @property
    def batch_minor(self) -> int:
        return sum(i.amount_minor for i in self.invoices)

    def view(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "run_on": self.run_on.isoformat(),
            "invoices": [
                {
                    "invoice_id": i.invoice_id,
                    "vendor_id": i.vendor_id,
                    "number": i.number,
                    "po_id": i.po_id,
                    "amount_minor": i.amount_minor,
                    "due_on": i.due_on.isoformat(),
                }
                for i in self.invoices
            ],
            "currency": "USD",
            "synthetic": True,
        }


__all__ = [
    "EXPECTED",
    "INVOICE_KINDS",
    "NEW_VENDOR_DAYS",
    "PRICE_TOLERANCE_PCT",
    "BankAccount",
    "Disposition",
    "Invoice",
    "InvoiceKind",
    "InvoiceTruth",
    "PaidInvoice",
    "PayablesCase",
    "PayablesTruth",
    "PurchaseOrder",
    "Receipt",
    "Vendor",
    "invoice_key",
]
