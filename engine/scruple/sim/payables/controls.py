"""Accounts payable controls, computed only from what the AP clerk can see.

The reference policy (`scruple.agents.payables_baselines`) applies these; the
verifier never does (it scores against the truth). All parameters are ASSUMPTIONS
(docs/WORKFLOWS.md AP-2 to AP-4).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta

from scruple.sim.payables.models import (
    NEW_VENDOR_DAYS,
    PRICE_TOLERANCE_PCT,
    Invoice,
    PaidInvoice,
    PurchaseOrder,
    Receipt,
    Vendor,
    invoice_key,
)


def match_problem(inv: Invoice, po: PurchaseOrder, receipts: Sequence[Receipt]) -> str | None:
    """Why a PO invoice fails the three-way match (invoice, PO, goods receipt), or None."""
    if po.vendor_id != inv.vendor_id:
        return "po belongs to another vendor"
    if not receipts:
        return "no goods received"
    if sum(r.qty for r in receipts) < inv.qty:
        return "fewer goods received than invoiced"
    if inv.qty > po.qty:
        return "more invoiced than ordered"
    if inv.unit_price_minor * 100 > po.unit_price_minor * (100 + PRICE_TOLERANCE_PCT):
        return f"price more than {PRICE_TOLERANCE_PCT}% over the po"
    return None


def is_duplicate(inv: Invoice, history: Iterable[PaidInvoice], earlier: Iterable[Invoice]) -> bool:
    """Same vendor, same normalised number and same amount as an invoice already paid
    or earlier in this batch (AP-3)."""
    key = (inv.vendor_id, invoice_key(inv.number), inv.amount_minor)
    seen = {(h.vendor_id, invoice_key(h.number), h.amount_minor) for h in history}
    seen |= {(e.vendor_id, invoice_key(e.number), e.amount_minor) for e in earlier}
    return key in seen


def is_new_vendor(v: Vendor, run_on: date) -> bool:
    return v.onboarded_on > run_on - timedelta(days=NEW_VENDOR_DAYS)


__all__ = ["is_duplicate", "is_new_vendor", "match_problem"]
