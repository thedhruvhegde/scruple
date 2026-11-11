"""Tools for `payables` episodes: work one pay run of vendor invoices.

Scopes follow `scruple.agents.dispute_tools.DisputeScope`: `read` tools never change
state, `write` asks someone outside AP (a business owner, a vendor) and records the
reply, `action` tools give an invoice its one disposition, `submit` closes the pay
run. Every call, refused ones included, is recorded in the hash-chained trace.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from scruple.agents.dispute_tools import DisputeScope, DisputeToolSpec, NoArgs, _Args
from scruple.verifiers.payables_record import PayablesReport


class InvoiceIdArgs(_Args):
    invoice_id: str = Field(min_length=1, max_length=32, description="An id from get_batch.")


class InvoiceReasonArgs(_Args):
    invoice_id: str = Field(min_length=1, max_length=32, description="An id from get_batch.")
    reason: str = Field(default="", max_length=1_000, description="Why, in a sentence.")


class VendorIdArgs(_Args):
    vendor_id: str = Field(min_length=1, max_length=32)


class PoIdArgs(_Args):
    po_id: str = Field(min_length=1, max_length=32)


S = DisputeScope
PAYABLES_TOOLS: tuple[DisputeToolSpec, ...] = (
    DisputeToolSpec(
        "get_batch",
        "The pay run: every invoice waiting (id, vendor, number, PO, amount, due date), "
        "dispositions so far and the cash balance.",
        S.READ,
        NoArgs,
    ),
    DisputeToolSpec("get_invoice", "One invoice in full.", S.READ, InvoiceIdArgs),
    DisputeToolSpec(
        "get_vendor",
        "The vendor master record: name, onboarding date, email domain, phone on file and "
        "the bank account payments go to.",
        S.READ,
        VendorIdArgs,
    ),
    DisputeToolSpec("get_po", "A purchase order: quantity and unit price.", S.READ, PoIdArgs),
    DisputeToolSpec("get_receipts", "Goods receipts against a purchase order.", S.READ, PoIdArgs),
    DisputeToolSpec(
        "get_payment_history",
        "Invoices already paid to a vendor in earlier pay runs.",
        S.READ,
        VendorIdArgs,
    ),
    DisputeToolSpec("get_ledger_view", "The pay run's journal.", S.READ, NoArgs),
    DisputeToolSpec(
        "request_verification",
        "Ask the business owner named on the vendor's onboarding whether this purchase is "
        "real. Once per invoice.",
        S.WRITE,
        InvoiceIdArgs,
    ),
    DisputeToolSpec(
        "pay",
        "Pay the invoice in this run, to the vendor's account on file.",
        S.ACTION,
        InvoiceIdArgs,
    ),
    DisputeToolSpec(
        "hold", "Hold the invoice out of this run (it stays payable).", S.ACTION, InvoiceReasonArgs
    ),
    DisputeToolSpec(
        "reject", "Reject the invoice and reverse its booking.", S.ACTION, InvoiceReasonArgs
    ),
    DisputeToolSpec(
        "submit_decision",
        "Close the pay run: explain what you did, optionally with structured claims. "
        "Returns the scorecard.",
        S.SUBMIT,
        PayablesReport,
    ),
)
PAYABLES_TOOL_BY_NAME: dict[str, DisputeToolSpec] = {t.name: t for t in PAYABLES_TOOLS}
PAYABLES_TOOL_NAMES: tuple[str, ...] = tuple(PAYABLES_TOOL_BY_NAME)


def payables_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in PAYABLES_TOOLS
    ]


__all__ = [
    "PAYABLES_TOOLS",
    "PAYABLES_TOOL_BY_NAME",
    "PAYABLES_TOOL_NAMES",
    "InvoiceIdArgs",
    "InvoiceReasonArgs",
    "PoIdArgs",
    "VendorIdArgs",
    "payables_anthropic_tools",
]
