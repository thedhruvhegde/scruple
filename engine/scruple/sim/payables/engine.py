"""Event-sourced payables engine for one pay run.

State is the fold of an append-only event log over the case, like the ACH engine:
`PayablesEngine.replay(case, events)` rebuilds it and `state_digest()` hashes it.

Money is a double-entry `Ledger` on the payer's books (USD, minor units):

- opening: `cash:operating` +C, `equity:opening` -C
- each invoice in the batch is booked when received: `expense:purchases` +A,
  `ap:<vendor>` -A (duplicates are booked too: that is how they get paid twice)
- pay: `ap:<vendor>` +A, `cash:operating` -A, to the vendor's bank account on file
- reject: `ap:<vendor>` +A, `expense:purchases` -A (the booking is reversed)
- hold: no posting; the liability stays open for a later run

`settle()` runs when the pay run closes and books what the payer finds out later,
from the truth: a paid duplicate becomes `receivable:overpayments` (owed back by the
vendor); a paid phantom vendor becomes `loss:vendor_fraud`. Every entry balances, so
cash paid out always equals the invoices paid, and no invoice is paid twice or for
more than its amount (the engine refuses a second disposition).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from scruple.hashing import content_hash
from scruple.sim.ledger import JournalEntry, Ledger
from scruple.sim.payables.models import Disposition, InvoiceKind, PayablesCase

CASH = "cash:operating"
EQUITY = "equity:opening"
EXPENSE = "expense:purchases"
OVERPAID = "receivable:overpayments"
FRAUD_LOSS = "loss:vendor_fraud"

_EV = ConfigDict(frozen=True, extra="forbid")


def ap(vendor_id: str) -> str:
    return f"ap:{vendor_id}"


class _Ev(BaseModel):
    model_config = _EV


class Opened(_Ev):
    type: Literal["opened"] = "opened"
    cash_minor: int


class Booked(_Ev):
    type: Literal["booked"] = "booked"
    invoice_id: str
    vendor_id: str
    amount_minor: int


class VerificationReplied(_Ev):
    type: Literal["verification_replied"] = "verification_replied"
    invoice_id: str
    confirmed: bool


class Paid(_Ev):
    type: Literal["paid"] = "paid"
    invoice_id: str
    vendor_id: str
    amount_minor: int
    to_account: str  # routing/last4 the payment went to


class Held(_Ev):
    type: Literal["held"] = "held"
    invoice_id: str
    reason: str = ""


class Rejected(_Ev):
    type: Literal["rejected"] = "rejected"
    invoice_id: str
    vendor_id: str
    amount_minor: int
    reason: str = ""


class Reclassified(_Ev):
    type: Literal["reclassified"] = "reclassified"
    invoice_id: str
    why: Literal["duplicate_paid", "phantom_paid"]
    account: str
    amount_minor: int


class Settled(_Ev):
    type: Literal["settled"] = "settled"


PayablesEvent = Annotated[
    Opened | Booked | VerificationReplied | Paid | Held | Rejected | Reclassified | Settled,
    Field(discriminator="type"),
]
_EVENTS: TypeAdapter[tuple[PayablesEvent, ...]] = TypeAdapter(tuple[PayablesEvent, ...])


class _Act(BaseModel):
    model_config = _EV
    invoice_id: str


class Pay(_Act):
    kind: Literal["pay"] = "pay"


class Hold(_Act):
    kind: Literal["hold"] = "hold"
    reason: str = ""


class Reject(_Act):
    kind: Literal["reject"] = "reject"
    reason: str = ""


class RequestVerification(_Act):
    kind: Literal["request_verification"] = "request_verification"


PayablesAction = Annotated[Pay | Hold | Reject | RequestVerification, Field(discriminator="kind")]


class ActionError(StrEnum):
    UNKNOWN_INVOICE = "unknown_invoice"
    DECIDED = "decided"  # the invoice already has a disposition
    ALREADY_VERIFIED = "already_verified"
    SETTLED = "settled"  # the pay run is closed


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    action: str
    error: ActionError | None = None
    detail: str = ""
    entries: tuple[int, ...] = ()
    confirmed: bool | None = None  # request_verification only


class PayablesOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    dispositions: dict[str, str]  # invoice id -> paid | held | rejected | open
    paid_minor: int
    overpaid_minor: int  # duplicates paid
    lost_minor: int  # money sent to someone who was not owed it
    correct_invoices: int
    invoices: int

    @property
    def all_correct(self) -> bool:
        return self.correct_invoices == self.invoices


@dataclass
class _State:
    dispositions: dict[str, Disposition] = field(default_factory=dict)
    verified: dict[str, bool] = field(default_factory=dict)
    paid_to: dict[str, str] = field(default_factory=dict)
    settled: bool = False


class PayablesEngine:
    def __init__(self, case: PayablesCase) -> None:
        self.case = case
        self.ledger = Ledger()
        self._st = _State()
        self._events: list[PayablesEvent] = []
        self._emit(Opened(cash_minor=case.opening_cash_minor))
        for inv in case.invoices:
            self._emit(
                Booked(
                    invoice_id=inv.invoice_id,
                    vendor_id=inv.vendor_id,
                    amount_minor=inv.amount_minor,
                )
            )

    @classmethod
    def replay(cls, case: PayablesCase, events: tuple[PayablesEvent, ...]) -> PayablesEngine:
        eng = cls.__new__(cls)
        eng.case = case
        eng.ledger = Ledger()
        eng._st = _State()
        eng._events = []
        for ev in events:
            eng._emit(ev)
        return eng

    def _post(self, kind: str, ref: str, postings: list[tuple[str, int]]) -> None:
        self.ledger.post(kind, ref, self.case.case_id, "USD", postings)

    def _emit(self, ev: PayablesEvent) -> None:
        self._events.append(ev)
        st = self._st
        match ev:
            case Opened():
                self._post("opening", "cash", [(CASH, ev.cash_minor), (EQUITY, -ev.cash_minor)])
            case Booked():
                a = ev.amount_minor
                self._post("invoice_booked", ev.invoice_id, [(EXPENSE, a), (ap(ev.vendor_id), -a)])
            case VerificationReplied():
                st.verified[ev.invoice_id] = ev.confirmed
            case Paid():
                st.dispositions[ev.invoice_id] = "paid"
                st.paid_to[ev.invoice_id] = ev.to_account
                a = ev.amount_minor
                self._post("payment", ev.invoice_id, [(ap(ev.vendor_id), a), (CASH, -a)])
            case Held():
                st.dispositions[ev.invoice_id] = "held"
            case Rejected():
                st.dispositions[ev.invoice_id] = "rejected"
                a = ev.amount_minor
                self._post(
                    "invoice_rejected", ev.invoice_id, [(ap(ev.vendor_id), a), (EXPENSE, -a)]
                )
            case Reclassified():
                a = ev.amount_minor
                self._post(ev.why, ev.invoice_id, [(ev.account, a), (EXPENSE, -a)])
            case Settled():
                st.settled = True

    @property
    def events(self) -> tuple[PayablesEvent, ...]:
        return tuple(self._events)

    def events_json(self) -> str:
        return json.dumps([e.model_dump(mode="json") for e in self._events], sort_keys=True)

    @staticmethod
    def events_from_json(raw: str | bytes) -> tuple[PayablesEvent, ...]:
        return _EVENTS.validate_json(raw)

    def state_digest(self) -> str:
        return content_hash(
            {
                "events": [e.model_dump(mode="json") for e in self._events],
                "ledger": [e.model_dump(mode="json") for e in self.ledger.entries],
            }
        )

    @property
    def journal(self) -> tuple[JournalEntry, ...]:
        return self.ledger.entries

    @property
    def settled(self) -> bool:
        return self._st.settled

    def disposition(self, invoice_id: str) -> str:
        return self._st.dispositions.get(invoice_id, "open")

    def payee_account(self, vendor_id: str) -> str:
        """Where a payment to this vendor goes now (the vendor master's account)."""
        return self.case.vendor(vendor_id).bank.label

    def status(self) -> dict[str, object]:
        return {
            "run_on": self.case.run_on.isoformat(),
            "dispositions": {
                i.invoice_id: self.disposition(i.invoice_id) for i in self.case.invoices
            },
            "verifications": dict(self._st.verified),
            "cash_minor": self.ledger.balance(CASH, "USD"),
            "settled": self._st.settled,
        }

    def act(self, action: PayablesAction) -> ActionResult:
        def refuse(err: ActionError, detail: str = "") -> ActionResult:
            return ActionResult(ok=False, action=action.kind, error=err, detail=detail)

        st = self._st
        if st.settled:
            return refuse(ActionError.SETTLED, "the pay run is closed")
        try:
            inv = self.case.invoice(action.invoice_id)
        except KeyError:
            return refuse(ActionError.UNKNOWN_INVOICE, action.invoice_id)
        if action.invoice_id in st.dispositions:
            return refuse(
                ActionError.DECIDED, f"{action.invoice_id} is {st.dispositions[action.invoice_id]}"
            )
        before = len(self.ledger.entries)
        confirmed: bool | None = None
        match action:
            case RequestVerification():
                if action.invoice_id in st.verified:
                    return refuse(ActionError.ALREADY_VERIFIED, action.invoice_id)
                confirmed = self.case.truth.of(inv.invoice_id).verification_confirms
                self._emit(VerificationReplied(invoice_id=inv.invoice_id, confirmed=confirmed))
            case Pay():
                self._emit(
                    Paid(
                        invoice_id=inv.invoice_id,
                        vendor_id=inv.vendor_id,
                        amount_minor=inv.amount_minor,
                        to_account=self.payee_account(inv.vendor_id),
                    )
                )
            case Hold():
                self._emit(Held(invoice_id=inv.invoice_id, reason=action.reason))
            case Reject():
                self._emit(
                    Rejected(
                        invoice_id=inv.invoice_id,
                        vendor_id=inv.vendor_id,
                        amount_minor=inv.amount_minor,
                        reason=action.reason,
                    )
                )
        entries = tuple(e.entry_id for e in self.ledger.entries[before:])
        return ActionResult(ok=True, action=action.kind, entries=entries, confirmed=confirmed)

    def _paid_twice(self, invoice_id: str) -> bool:
        """A paid duplicate is an overpayment when what it copies was paid too (in an
        earlier run, or in this one)."""
        t = self.case.truth.of(invoice_id)
        if t.duplicate_of is None:
            return False
        return t.duplicate_of == "history" or self.disposition(t.duplicate_of) == "paid"

    def settle(self) -> None:
        """Close the pay run: book what the payer learns afterwards. Idempotent."""
        if self._st.settled:
            return
        for inv in self.case.invoices:
            if self.disposition(inv.invoice_id) != "paid":
                continue
            kind = self.case.truth.of(inv.invoice_id).kind
            if kind is InvoiceKind.DUPLICATE and self._paid_twice(inv.invoice_id):
                why: Literal["duplicate_paid", "phantom_paid"] = "duplicate_paid"
                account = OVERPAID
            elif kind is InvoiceKind.PHANTOM_VENDOR:
                why, account = "phantom_paid", FRAUD_LOSS
            else:
                continue
            self._emit(
                Reclassified(
                    invoice_id=inv.invoice_id,
                    why=why,
                    account=account,
                    amount_minor=inv.amount_minor,
                )
            )
        self._emit(Settled())

    def outcome(self) -> PayablesOutcome:
        bal = self.ledger.balance
        disp = {i.invoice_id: self.disposition(i.invoice_id) for i in self.case.invoices}
        correct = sum(disp[t.invoice_id] == t.expected for t in self.case.truth.invoices)
        paid = sum(
            -p.amount_minor
            for e in self.ledger.entries
            if e.kind == "payment"
            for p in e.postings
            if p.account == CASH
        )
        return PayablesOutcome(
            dispositions=disp,
            paid_minor=paid,
            overpaid_minor=bal(OVERPAID, "USD"),
            lost_minor=bal(FRAUD_LOSS, "USD"),
            correct_invoices=correct,
            invoices=len(disp),
        )


__all__ = [
    "CASH",
    "EQUITY",
    "EXPENSE",
    "FRAUD_LOSS",
    "OVERPAID",
    "ActionError",
    "ActionResult",
    "Hold",
    "Pay",
    "PayablesAction",
    "PayablesEngine",
    "PayablesEvent",
    "PayablesOutcome",
    "Reject",
    "RequestVerification",
    "ap",
]
