"""Payables environment: generator, controls, engine and ledger invariants."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.sim.payables.controls import is_duplicate, is_new_vendor, match_problem
from scruple.sim.payables.engine import (
    CASH,
    ActionError,
    Hold,
    Pay,
    PayablesEngine,
    Reject,
    RequestVerification,
)
from scruple.sim.payables.generator import generate_payables_case
from scruple.sim.payables.models import (
    EXPECTED,
    INVOICE_KINDS,
    Invoice,
    InvoiceKind,
    PayablesCase,
    invoice_key,
)

K = InvoiceKind


@pytest.mark.parametrize(
    ("a", "b"),
    [("INV-001001", "1001"), ("inv 1001", "INV1001"), ("INV_0001001", "1001")],
)
def test_invoice_keys_ignore_formatting(a: str, b: str) -> None:
    assert invoice_key(a) == invoice_key(b)


def test_recurring_numbers_stay_distinct() -> None:
    assert invoice_key("SUB-2026-03") != invoice_key("SUB-2026-04")


@pytest.mark.parametrize("kind", INVOICE_KINDS)
@pytest.mark.parametrize("difficulty", [0.0, 1.0])
def test_cases_are_deterministic_and_carry_the_kind(kind: InvoiceKind, difficulty: float) -> None:
    a = generate_payables_case(3, difficulty, [kind])
    assert a == generate_payables_case(3, difficulty, [kind])
    assert a != generate_payables_case(4, difficulty, [kind])
    assert kind in {t.kind for t in a.truth.invoices}
    assert all(t.expected == EXPECTED[t.kind] for t in a.truth.invoices)


def test_bad_inputs_are_refused() -> None:
    with pytest.raises(ValueError):
        generate_payables_case(1, 2.0)
    with pytest.raises(ValueError):
        generate_payables_case(1, 0.5, ["nope"])


def test_view_hides_the_truth() -> None:
    c = generate_payables_case(5, 0.5, list(INVOICE_KINDS))
    text = json.dumps(c.view()).lower()
    assert "truth" not in text and "phantom" not in text and "duplicate" not in text


def observable_reading(c: PayablesCase, inv: Invoice, earlier: list[Invoice]) -> str:
    """What the controls say, kind by kind (the reference applies the same)."""
    if is_duplicate(inv, c.history, earlier):
        return "rejected"
    if inv.po_id is None:
        if is_new_vendor(c.vendor(inv.vendor_id), c.run_on):
            return "verify"
        return "paid"
    problem = match_problem(inv, c.po(inv.po_id), c.receipts_for(inv.po_id))
    return "held" if problem else "paid"


@given(
    seed=st.integers(0, 10_000),
    d=st.floats(0, 1),
    kinds=st.lists(st.sampled_from(INVOICE_KINDS), max_size=3),
)
@settings(max_examples=150, deadline=None)
def test_controls_separate_every_kind(seed: int, d: float, kinds: list[InvoiceKind]) -> None:
    c = generate_payables_case(seed, d, kinds)
    for i, inv in enumerate(c.invoices):
        t = c.truth.of(inv.invoice_id)
        got = observable_reading(c, inv, list(c.invoices[:i]))
        if got == "verify":
            assert t.kind in (K.PHANTOM_VENDOR, K.NEW_VENDOR)
            got = "paid" if t.verification_confirms else "rejected"
        assert got == t.expected, (t.kind, inv)


# --- engine ---------------------------------------------------------------------------
def test_each_invoice_takes_one_disposition() -> None:
    c = generate_payables_case(1, 0.5, [K.CLEAN])
    eng = PayablesEngine(c)
    iid = c.invoices[0].invoice_id
    assert eng.act(Pay(invoice_id=iid)).ok
    again = eng.act(Reject(invoice_id=iid))
    assert again.error is ActionError.DECIDED
    assert eng.act(Pay(invoice_id="AP-99")).error is ActionError.UNKNOWN_INVOICE
    assert eng.act(RequestVerification(invoice_id=iid)).error is ActionError.DECIDED


def test_verification_answers_from_the_truth_once() -> None:
    c = generate_payables_case(2, 0.5, [K.PHANTOM_VENDOR, K.NEW_VENDOR])
    eng = PayablesEngine(c)
    for t in c.truth.invoices:
        if t.kind in (K.PHANTOM_VENDOR, K.NEW_VENDOR):
            r = eng.act(RequestVerification(invoice_id=t.invoice_id))
            assert r.confirmed is (t.kind is K.NEW_VENDOR)
            again = eng.act(RequestVerification(invoice_id=t.invoice_id))
            assert again.error is ActionError.ALREADY_VERIFIED


def test_paid_duplicates_and_phantoms_are_found_at_settlement() -> None:
    c = generate_payables_case(4, 1.0, [K.DUPLICATE, K.PHANTOM_VENDOR])
    eng = PayablesEngine(c)
    for inv in c.invoices:
        eng.act(Pay(invoice_id=inv.invoice_id))
    eng.settle()
    eng.settle()  # idempotent
    o = eng.outcome()
    dup = [inv for inv in c.invoices if c.truth.of(inv.invoice_id).kind is K.DUPLICATE]
    ph = [inv for inv in c.invoices if c.truth.of(inv.invoice_id).kind is K.PHANTOM_VENDOR]
    assert o.overpaid_minor == sum(i.amount_minor for i in dup) > 0
    assert o.lost_minor == sum(i.amount_minor for i in ph) > 0
    assert eng.act(Hold(invoice_id=c.invoices[0].invoice_id)).error is ActionError.SETTLED


def test_replay_rebuilds_the_engine() -> None:
    c = generate_payables_case(6, 0.5)
    eng = PayablesEngine(c)
    eng.act(Pay(invoice_id=c.invoices[0].invoice_id))
    eng.act(Hold(invoice_id=c.invoices[-1].invoice_id, reason="check"))
    eng.settle()
    again = PayablesEngine.replay(c, PayablesEngine.events_from_json(eng.events_json()))
    assert again.state_digest() == eng.state_digest() and again.outcome() == eng.outcome()


actions = st.lists(
    st.tuples(st.sampled_from(["pay", "hold", "reject", "verify"]), st.integers(0, 12)),
    max_size=20,
)


@given(seed=st.integers(0, 5_000), d=st.floats(0, 1), moves=actions)
@settings(max_examples=120, deadline=None)
def test_ledger_stays_balanced_and_no_money_is_invented(
    seed: int, d: float, moves: list[tuple[str, int]]
) -> None:
    c = generate_payables_case(seed, d)
    eng = PayablesEngine(c)
    make = {"pay": Pay, "hold": Hold, "reject": Reject, "verify": RequestVerification}
    for name, i in moves:
        eng.act(make[name](invoice_id=c.invoices[i % len(c.invoices)].invoice_id))
    eng.settle()
    assert all(sum(p.amount_minor for p in e.postings) == 0 for e in eng.journal)
    totals: dict[str, int] = {}
    for e in eng.journal:
        for p in e.postings:
            totals[p.account] = totals.get(p.account, 0) + p.amount_minor
    assert sum(totals.values()) == 0
    o = eng.outcome()
    paid = [inv for inv in c.invoices if o.dispositions[inv.invoice_id] == "paid"]
    assert o.paid_minor == sum(i.amount_minor for i in paid)  # cash out = invoices paid
    assert c.opening_cash_minor - eng.ledger.balance(CASH, "USD") == o.paid_minor
    payments = [e for e in eng.journal if e.kind == "payment"]
    assert len({e.txn_id for e in payments}) == len(payments)  # nobody paid twice
    for v in c.vendors:  # never paid a vendor more than was booked to it
        assert totals.get(f"ap:{v.vendor_id}", 0) <= 0
    assert o.overpaid_minor + o.lost_minor <= o.paid_minor
