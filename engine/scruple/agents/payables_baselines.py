"""Scripted policies for `payables` episodes: the reference clerk and strawmen.

`reference_policy` reads only what the tools return (never the truth) and applies
`scruple.sim.payables.controls`, invoice by invoice in batch order: reject a
duplicate (same vendor, number and amount as one already paid or earlier in the
batch); for a non-PO invoice from a vendor onboarded in the last 30 days, ask the
business owner and pay or reject on the answer; pay other non-PO invoices; hold a PO
invoice that fails the three-way match and pay the rest. On every generated case it
ends with the truth's dispositions (tests pin this).

Strawmen: `pay_everything`, `hold_everything` (both read first) and `match_only`
(three-way match, no duplicate or vendor checks).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import date
from typing import Any

from scruple.agents.errors import EpisodeError
from scruple.agents.payables_episode import PayablesEpisode
from scruple.sim.payables.controls import is_duplicate, is_new_vendor, match_problem
from scruple.sim.payables.models import (
    Invoice,
    PaidInvoice,
    PurchaseOrder,
    Receipt,
    Vendor,
)
from scruple.verifiers.payables_record import PayablesClaims, PayablesReport

PayablesPolicy = Callable[[PayablesEpisode], None]
ReportFn = Callable[[PayablesEpisode], PayablesReport]


def _vendor(view: dict[str, Any]) -> Vendor:
    return Vendor.model_validate({k: v for k, v in view.items() if k != "synthetic"})


def _invoice(view: dict[str, Any]) -> Invoice:
    return Invoice.model_validate(
        {k: v for k, v in view.items() if k not in ("amount_minor", "currency")}
    )


def check(ep: PayablesEpisode, iid: str) -> tuple[Invoice, Vendor, list[PaidInvoice], str | None]:
    """Read everything about one invoice; return it, its vendor, the vendor's history
    and the three-way match problem (None for a non-PO invoice or a clean match)."""
    inv = _invoice(ep.call("get_invoice", {"invoice_id": iid})["invoice"])
    vendor = _vendor(ep.call("get_vendor", {"vendor_id": inv.vendor_id})["vendor"])
    hist = ep.call("get_payment_history", {"vendor_id": inv.vendor_id})["paid"]
    history = [PaidInvoice.model_validate(h) for h in hist]
    problem = None
    if inv.po_id is not None:
        po = PurchaseOrder.model_validate(ep.call("get_po", {"po_id": inv.po_id})["purchase_order"])
        rs = ep.call("get_receipts", {"po_id": inv.po_id})["receipts"]
        problem = match_problem(inv, po, [Receipt.model_validate(r) for r in rs])
    return inv, vendor, history, problem


def reference_policy(ep: PayablesEpisode) -> None:
    batch = ep.call("get_batch")["batch"]
    run_on = date.fromisoformat(batch["run_on"])
    earlier: list[Invoice] = []
    for row in batch["invoices"]:
        iid = row["invoice_id"]
        inv, vendor, history, problem = check(ep, iid)
        if is_duplicate(inv, history, earlier):
            ep.call(
                "reject",
                {
                    "invoice_id": iid,
                    "reason": "duplicate of an invoice already paid or in this run",
                },
            )
        elif inv.po_id is None and is_new_vendor(vendor, run_on):
            got = ep.call("request_verification", {"invoice_id": iid})
            if got["confirmed"]:
                ep.call("pay", {"invoice_id": iid})
            else:
                ep.call("reject", {"invoice_id": iid, "reason": "business owner did not confirm"})
        elif problem is not None:
            ep.call("hold", {"invoice_id": iid, "reason": problem})
        else:
            ep.call("pay", {"invoice_id": iid})
        earlier.append(inv)


def _each(ep: PayablesEpisode, tool: str) -> None:
    batch = ep.call("get_batch")["batch"]
    for row in batch["invoices"]:
        check(ep, row["invoice_id"])
        ep.call(tool, {"invoice_id": row["invoice_id"]})


def pay_everything(ep: PayablesEpisode) -> None:
    _each(ep, "pay")


def hold_everything(ep: PayablesEpisode) -> None:
    _each(ep, "hold")


def match_only(ep: PayablesEpisode) -> None:
    batch = ep.call("get_batch")["batch"]
    for row in batch["invoices"]:
        iid = row["invoice_id"]
        _, _, _, problem = check(ep, iid)
        ep.call("hold" if problem else "pay", {"invoice_id": iid})


def honest_report(ep: PayablesEpisode) -> PayablesReport:
    o = ep.engine.outcome()
    counts = {k: sum(v == k for v in o.dispositions.values()) for k in ("paid", "held", "rejected")}
    return PayablesReport(
        explanation="Worked the pay run invoice by invoice; see the trace.",
        claims=PayablesClaims(
            paid_minor=o.paid_minor,
            invoices_paid=counts["paid"],
            invoices_held=counts["held"],
            invoices_rejected=counts["rejected"],
        ),
    )


def run_payables_policy(
    policy: PayablesPolicy,
    seed: int,
    case_type: str | None = None,
    *,
    report: ReportFn = honest_report,
    reference: bool = False,
) -> PayablesEpisode:
    ep = PayablesEpisode(seed, case_type, reference=reference)
    with contextlib.suppress(EpisodeError):  # a refused call ends the script
        policy(ep)
    if not ep.closed:
        ep.call("submit_decision", report(ep).model_dump(mode="json"))
    return ep


BASELINES: dict[str, PayablesPolicy] = {
    "reference": reference_policy,
    "pay_everything": pay_everything,
    "hold_everything": hold_everything,
    "match_only": match_only,
}

__all__ = [
    "BASELINES",
    "PayablesPolicy",
    "check",
    "hold_everything",
    "honest_report",
    "match_only",
    "pay_everything",
    "reference_policy",
    "run_payables_policy",
]
