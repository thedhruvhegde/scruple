"""Seeded generator of payables pay runs, with one difficulty knob.

`generate_payables_case(seed, difficulty, kinds)` is a pure function of its arguments.
Vendors, numbers, dates, amounts and bank details are made up. A batch holds four to
nine invoices from three to five established vendors (plus any new ones the kinds
need); `kinds` forces invoice kinds into the batch, otherwise the seed picks a mix.

Difficulty in [0, 1] (all ASSUMPTION AP-6, ours): more invoices and more traps;
clean invoices priced closer to the tolerance; price variances just over it; short
receipts short by one unit; duplicate numbers written in more different ways; phantom
invoices with less round amounts; impostor bank change requests that come from the
vendor's real (compromised) mailbox instead of a lookalike domain, with no urgency.

`changes` adds bank detail change requests (`CHANGE_KINDS`); without it, a batch with
no forced kinds gets one change request with probability 0.4, drawn from a separate
random stream so the invoices are the same with or without it.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from scruple.hashing import sha256_hex
from scruple.sim.payables.controls import is_new_vendor
from scruple.sim.payables.models import (
    EXPECTED,
    INVOICE_KINDS,
    BankAccount,
    ChangeRequest,
    ChangeTruth,
    Invoice,
    InvoiceKind,
    InvoiceTruth,
    PaidInvoice,
    PayablesCase,
    PayablesTruth,
    PurchaseOrder,
    Receipt,
    Vendor,
)

K = InvoiceKind
BASE_DATE = date(2026, 1, 12)
GOODS = ("copier toner", "safety gloves", "pallet wrap", "laptop docks", "printer paper")
TRAPS = frozenset(INVOICE_KINDS) - {K.CLEAN, K.RECURRING}
CHANGE_KINDS = ("bank_change_fraud", "bank_change_genuine")
CHANGE_RATE = 0.4  # AP-8


def kind_weights(difficulty: float) -> dict[InvoiceKind, float]:
    return {k: (0.3 + 0.7 * difficulty if k in TRAPS else 2.0) for k in INVOICE_KINDS}


@dataclass
class _B:
    rng: random.Random
    run_on: date
    difficulty: float
    vendors: list[Vendor] = field(default_factory=list)
    pos: list[PurchaseOrder] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    history: list[PaidInvoice] = field(default_factory=list)
    used_numbers: set[tuple[str, str]] = field(default_factory=set)
    n: int = 0

    def nid(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}-{self.n:03d}"

    def digits(self, k: int) -> str:
        return "".join(str(self.rng.randrange(10)) for _ in range(k))

    def vendor(self, new: bool = False) -> Vendor:
        vid = self.nid("V")
        stem = f"synth-{self.rng.choice(('supply', 'parts', 'logistics', 'office', 'tools'))}"
        stem += f"-{self.rng.randint(10, 99)}"
        days = self.rng.randint(3, 25) if new else self.rng.randint(200, 3_000)
        v = Vendor(
            vendor_id=vid,
            name=stem.replace("-", " ").upper(),
            onboarded_on=self.run_on - timedelta(days=days),
            email_domain=f"{stem}.example",
            phone_on_file=f"+1-555-{self.digits(3)}-{self.digits(4)}",
            bank=BankAccount(routing=self.digits(9), account_last4=self.digits(4)),
        )
        self.vendors.append(v)
        return v

    def number(self, vendor_id: str) -> int:
        while True:
            n = self.rng.randint(1_000, 99_999)
            if (vendor_id, str(n)) not in self.used_numbers:
                self.used_numbers.add((vendor_id, str(n)))
                return n

    def fmt(self, n: int, variant: int) -> str:
        return (f"INV-{n:06d}", f"INV{n}", f"{n}", f"inv {n}", f"INV_{n:07d}")[variant % 5]


def _po_invoice(
    b: _B, v: Vendor, iid: str, *, price_up: float, recv: int | None, number: str | None = None
) -> Invoice:
    rng = b.rng
    qty = rng.randint(2, 40)
    price = rng.randint(5, 900) * 100 + rng.choice((0, 50, 99))
    po = PurchaseOrder(
        po_id=b.nid("PO"),
        vendor_id=v.vendor_id,
        description=rng.choice(GOODS),
        qty=qty + rng.choice((0, 0, rng.randint(1, 10))),
        unit_price_minor=price,
        issued_on=b.run_on - timedelta(days=rng.randint(20, 60)),
    )
    b.pos.append(po)
    if recv is not None:
        b.receipts.append(
            Receipt(
                receipt_id=b.nid("GR"),
                po_id=po.po_id,
                qty=recv if recv > 0 else qty,
                received_on=po.issued_on + timedelta(days=rng.randint(3, 15)),
            )
        )
    inv_price = price + int(price_up)
    issued = b.run_on - timedelta(days=rng.randint(5, 25))
    return Invoice(
        invoice_id=iid,
        vendor_id=v.vendor_id,
        number=number or b.fmt(b.number(v.vendor_id), rng.randrange(5)),
        po_id=po.po_id,
        description=po.description,
        qty=qty,
        unit_price_minor=inv_price,
        issued_on=issued,
        due_on=issued + timedelta(days=30),
    )


def _make(
    b: _B, kind: InvoiceKind, vendors: Sequence[Vendor], iid: str
) -> list[tuple[Invoice, InvoiceTruth]]:
    rng, d = b.rng, b.difficulty
    v = rng.choice(list(vendors))
    out: list[tuple[Invoice, InvoiceTruth]] = []

    def truth(inv: Invoice, k: InvoiceKind, **kw: object) -> tuple[Invoice, InvoiceTruth]:
        return inv, InvoiceTruth(invoice_id=inv.invoice_id, kind=k, expected=EXPECTED[k], **kw)  # type: ignore[arg-type]

    def priced(inv: Invoice, uplift: int) -> Invoice:
        return inv.model_copy(update={"unit_price_minor": inv.unit_price_minor + uplift})

    match kind:
        case K.CLEAN | K.PRICE_VARIANCE | K.SHORT_RECEIPT | K.NO_RECEIPT:
            recv: int | None = 0  # 0: the full quantity
            inv = _po_invoice(b, v, iid, price_up=0, recv=None)
            po = b.pos[-1]
            p = po.unit_price_minor
            if kind is K.CLEAN:
                uplift = int(p * 0.02 * rng.random()) if rng.random() < d else 0
            elif kind is K.PRICE_VARIANCE:
                uplift = -(-p * 2 // 100) + 1 + rng.randint(0, round(p * 0.2 * (1 - d)))
            else:
                uplift = 0
            inv = priced(inv, uplift)
            if kind is K.SHORT_RECEIPT:
                recv = max(1, inv.qty - (1 if rng.random() < d else rng.randint(1, inv.qty - 1)))
            elif kind is K.NO_RECEIPT:
                recv = None
            if recv is not None:
                b.receipts.append(
                    Receipt(
                        receipt_id=b.nid("GR"),
                        po_id=po.po_id,
                        qty=recv or inv.qty,
                        received_on=po.issued_on + timedelta(days=rng.randint(3, 15)),
                    )
                )
            out.append(truth(inv, kind))
        case K.RECURRING:
            month = b.run_on.replace(day=1)
            amount = rng.randint(80, 3_000) * 100
            for back in (1, 2, 3):
                prev = (month - timedelta(days=28 * back)).replace(day=1)
                b.history.append(
                    PaidInvoice(
                        vendor_id=v.vendor_id,
                        number=f"SUB-{prev:%Y-%m}",
                        amount_minor=amount,
                        paid_on=prev + timedelta(days=20),
                    )
                )
            inv = Invoice(
                invoice_id=iid,
                vendor_id=v.vendor_id,
                number=f"SUB-{month:%Y-%m}",
                po_id=None,
                description="monthly service subscription",
                qty=1,
                unit_price_minor=amount,
                issued_on=month,
                due_on=month + timedelta(days=30),
            )
            out.append(truth(inv, kind))
        case K.DUPLICATE:
            n = b.number(v.vendor_id)
            original = _po_invoice(b, v, iid + "a", price_up=0, recv=0, number=b.fmt(n, 0))
            copy_number = b.fmt(n, 1 + rng.randrange(4 if d > 0.5 else 1))
            if rng.random() < 0.5:  # paid in an earlier run
                b.history.append(
                    PaidInvoice(
                        vendor_id=v.vendor_id,
                        number=original.number,
                        amount_minor=original.amount_minor,
                        paid_on=b.run_on - timedelta(days=rng.randint(7, 40)),
                    )
                )
                dup = original.model_copy(update={"invoice_id": iid, "number": copy_number})
                out.append(truth(dup, kind, duplicate_of="history"))
            else:  # both copies in this batch: the original first
                orig = original.model_copy(update={"invoice_id": iid})
                dup = original.model_copy(
                    update={
                        "invoice_id": iid + "-2",
                        "number": copy_number,
                        "issued_on": original.issued_on + timedelta(days=rng.randint(0, 6)),
                    }
                )
                out.append(truth(orig, K.CLEAN))
                out.append(truth(dup, kind, duplicate_of=iid))
        case K.PHANTOM_VENDOR | K.NEW_VENDOR:
            nv = b.vendor(new=True)
            if kind is K.PHANTOM_VENDOR:
                price = (
                    rng.randint(2, 15) * 100_000
                    if rng.random() > d
                    else rng.randint(150_000, 1_500_000)
                )
                desc = rng.choice(
                    ("consulting services", "advisory retainer", "marketing services")
                )
            else:
                price = rng.randint(30_000, 900_000) + rng.choice((0, 37, 81))
                desc = rng.choice(("emergency roof repair", "replacement pump", "site cleanup"))
            issued = b.run_on - timedelta(days=rng.randint(1, 10))
            inv = Invoice(
                invoice_id=iid,
                vendor_id=nv.vendor_id,
                number=b.fmt(b.number(nv.vendor_id), rng.randrange(5)),
                po_id=None,
                description=desc,
                qty=1,
                unit_price_minor=price,
                issued_on=issued,
                due_on=issued + timedelta(days=rng.choice((7, 15, 30))),
            )
            out.append(truth(inv, kind, verification_confirms=kind is K.NEW_VENDOR))
    return out


def _lookalike(rng: random.Random, domain: str) -> str:
    stem = domain.removesuffix(".example")
    swaps = [(a, b) for a, b in (("l", "1"), ("o", "0"), ("i", "l"), ("s", "z")) if a in stem]
    if swaps and rng.random() < 0.7:
        a, b = rng.choice(swaps)
        i = stem.index(a)
        return stem[:i] + b + stem[i + 1 :] + ".example"
    return stem + "-billing.example"


def _change(
    b: _B,
    rng: random.Random,
    change_id: str,
    fraud: bool,
    pairs: Sequence[tuple[Invoice, InvoiceTruth]],
) -> tuple[ChangeRequest, ChangeTruth]:
    """One bank detail change request for a vendor with an invoice due in this run."""
    payable = [i.vendor_id for i, t in pairs if t.expected == "paid" and t.duplicate_of is None]
    old = {v.vendor_id for v in b.vendors if not is_new_vendor(v, b.run_on)}
    pool = sorted(set(payable) & old) or sorted(old)
    pick = rng.choice(pool)
    v = next(x for x in b.vendors if x.vendor_id == pick)
    d = b.difficulty
    new_bank = BankAccount(routing=b.digits(9), account_last4=b.digits(4))
    phone = f"+1-555-{b.digits(3)}-{b.digits(4)}"
    if fraud and rng.random() > d:  # a lookalike domain, a free-mail reply-to, urgency
        sender = f"accounts@{_lookalike(rng, v.email_domain)}"
        reply_to = f"{v.email_domain.split('.')[0]}.payments@synth-mail.example"
        subject = "URGENT: updated bank details before today's payment"
        body = "Our bank account is under audit. Pay only the new account below, today."
        in_request = phone
    elif fraud:  # the vendor's real mailbox, compromised: only a call-back shows it
        sender = reply_to = f"accounts@{v.email_domain}"
        subject = "Updated remittance details"
        body = "Please note our new account for remittances from this month."
        in_request = phone if rng.random() < 0.5 else ""
    else:
        sender = reply_to = f"accounts@{v.email_domain}"
        subject = "New bank account for payments"
        body = "We moved our banking. Please update your records."
        if rng.random() < d:
            body += " Kindly update before your next pay run."
        in_request = v.phone_on_file if rng.random() < 0.5 else ""
    req = ChangeRequest(
        change_id=change_id,
        vendor_id=v.vendor_id,
        received_on=b.run_on - timedelta(days=rng.randint(1, 5)),
        from_address=sender,
        reply_to=reply_to,
        phone_in_request=in_request,
        new_bank=new_bank,
        subject=subject,
        body=body,
    )
    return req, ChangeTruth(
        change_id=change_id, fraudulent=fraud, expected="declined" if fraud else "applied"
    )


def generate_payables_case(
    seed: int,
    difficulty: float = 0.5,
    kinds: Sequence[InvoiceKind | str] | None = None,
    case_id: str | None = None,
    changes: Sequence[str] | None = None,
) -> PayablesCase:
    if not 0.0 <= difficulty <= 1.0:
        raise ValueError("difficulty must be in [0, 1]")
    want = tuple(InvoiceKind(k) for k in kinds) if kinds else ()
    key = "+".join(k.value for k in want) or None
    rng = random.Random(f"payables:{seed}:{key}:{difficulty:.6f}")
    run_on = BASE_DATE + timedelta(days=rng.randrange(300))
    b = _B(rng, run_on, difficulty)
    established = [b.vendor() for _ in range(rng.randint(3, 5))]
    for v in established:  # unrelated history: earlier runs
        for _ in range(rng.randint(1, 3)):
            b.history.append(
                PaidInvoice(
                    vendor_id=v.vendor_id,
                    number=b.fmt(b.number(v.vendor_id), rng.randrange(5)),
                    amount_minor=rng.randint(50, 5_000) * 100,
                    paid_on=run_on - timedelta(days=rng.randint(10, 90)),
                )
            )
    n = rng.randint(4, 6) + round(3 * difficulty)
    w = kind_weights(difficulty)
    picks = list(want) + rng.choices(list(w), weights=list(w.values()), k=max(0, n - len(want)))
    units = [_make(b, k, established, f"AP-{i + 1:02d}") for i, k in enumerate(picks)]
    rng.shuffle(units)
    pairs = [p for u in units for p in u]
    invoices = tuple(inv for inv, _ in pairs)
    batch = sum(i.amount_minor for i in invoices)
    cid = case_id or "ap-" + sha256_hex(f"case:{seed}:{key}:{difficulty}")[:12]
    crng = random.Random(f"payables-change:{seed}:{key}:{difficulty:.6f}")
    if changes is None:
        changes = (
            () if want else ((crng.choice(CHANGE_KINDS),) if crng.random() < CHANGE_RATE else ())
        )
    requests, change_truths = [], []
    for j, ck in enumerate(changes):
        if ck not in CHANGE_KINDS:
            raise ValueError(f"unknown change kind {ck!r}")
        req, t = _change(b, crng, f"CR-{j + 1:02d}", ck == "bank_change_fraud", pairs)
        requests.append(req)
        change_truths.append(t)
    return PayablesCase(
        case_id=cid,
        run_on=run_on,
        vendors=tuple(b.vendors),
        purchase_orders=tuple(b.pos),
        receipts=tuple(b.receipts),
        invoices=invoices,
        history=tuple(sorted(b.history, key=lambda h: (h.paid_on, h.vendor_id, h.number))),
        change_requests=tuple(requests),
        opening_cash_minor=batch * 2 + rng.randint(10_000, 500_000) * 100,
        difficulty=difficulty,
        truth=PayablesTruth(invoices=tuple(t for _, t in pairs), changes=tuple(change_truths)),
    )


__all__ = ["BASE_DATE", "CHANGE_KINDS", "TRAPS", "generate_payables_case", "kind_weights"]
