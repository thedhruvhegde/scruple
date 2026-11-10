"""Reconciliation environment: statement files round trip through the real parsers, the
seeded generator injects every break kind, and the event-sourced engine never invents
money on its own."""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.ingest.bank import parse_bai2, parse_camt053
from scruple.ingest.errors import IngestError
from scruple.sim.recon.engine import (
    CASH,
    FEE_ACCOUNT,
    SUSPENSE,
    Adjust,
    Escalate,
    MarkTiming,
    Match,
    ReconAction,
    ReconEngine,
    ReconError,
    Unmatch,
)
from scruple.sim.recon.generator import TIMING_DAYS, generate_recon_case
from scruple.sim.recon.models import BREAK_KINDS, BankLine, BreakKind
from scruple.sim.recon.statement import bai2_code, read_statement, write_bai2, write_camt053

K = BreakKind
FORMATS = ("camt053", "bai2")


# --- statement files --------------------------------------------------------------------
@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("difficulty", [0.0, 0.5, 1.0])
def test_statement_round_trips_through_the_parsers(fmt: str, difficulty: float) -> None:
    for seed in range(12):
        c = generate_recon_case(seed, difficulty, fmt, kinds=tuple(BREAK_KINDS))
        p = read_statement(c.statement_format, c.statement_file)
        assert p.opening_balance_minor == c.opening_balance_minor
        assert p.closing_balance_minor == c.closing_balance_minor
        assert p.currency == c.currency and p.account_id == c.account_id
        assert len(p.lines) == len(c.bank_lines)
        for got, want in zip(p.lines, c.bank_lines, strict=True):
            code = want.type_code if fmt == "camt053" else bai2_code(want)
            assert (got.line_id, got.booking_date, got.amount_minor) == (
                want.line_id,
                want.booking_date,
                want.amount_minor,
            )
            assert (got.reference, got.bank_ref, got.type_code, got.description) == (
                want.reference,
                want.bank_ref,
                code,
                want.description,
            )


def test_files_are_marked_synthetic_and_parse_with_integrity_checks() -> None:
    camt = generate_recon_case(1, 0.5, "camt053")
    assert "SYNTHETIC" in camt.statement_file
    assert parse_camt053(camt.statement_file)[0].format == "camt053"
    bai = generate_recon_case(1, 0.5, "bai2")
    stmts = parse_bai2(bai.statement_file)  # control totals and counts checked
    assert len(stmts) == len({b.booking_date for b in bai.bank_lines})


def test_a_tampered_bai2_total_is_refused() -> None:
    bai = generate_recon_case(2, 0.5, "bai2").statement_file
    lines = bai.splitlines()
    i = next(n for n, x in enumerate(lines) if x.startswith("16,"))
    parts = lines[i].split(",")
    parts[2] = str(int(parts[2]) + 1)
    lines[i] = ",".join(parts)
    with pytest.raises(IngestError):
        parse_bai2("\n".join(lines))


@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY"])
def test_writers_handle_currency_exponents(currency: str) -> None:
    line = BankLine(
        line_id="S-001",
        booking_date=date(2026, 3, 2),
        amount_minor=-12345,
        reference="PAY-12345A",
        bank_ref="BK1",
        type_code="PYMT",
        description="SUPPLIER PAYMENT",
    )
    period = (date(2026, 3, 1), date(2026, 3, 28))
    p = read_statement("camt053", write_camt053("ID", "ACCT-1", currency, period, 100_000, [line]))
    assert p.lines[0].amount_minor == -12345 and p.closing_balance_minor == 100_000 - 12345
    b = read_statement("bai2", write_bai2("BANK", "12345678", currency, period[1], 100_000, [line]))
    assert b.lines[0].amount_minor == -12345 and b.closing_balance_minor == 100_000 - 12345


def test_unknown_format() -> None:
    with pytest.raises(ValueError, match="format"):
        read_statement("mt940", "")
    with pytest.raises(ValueError, match="format"):
        generate_recon_case(1, 0.5, "mt940")


# --- generator --------------------------------------------------------------------------
def test_generator_is_deterministic() -> None:
    assert generate_recon_case(5, 0.3) == generate_recon_case(5, 0.3)
    assert generate_recon_case(5, 0.3) != generate_recon_case(6, 0.3)


@pytest.mark.parametrize("kind", BREAK_KINDS)
def test_every_break_kind_is_injected_with_a_treatment(kind: BreakKind) -> None:
    for seed in range(10):
        c = generate_recon_case(seed, 0.5, kinds=(kind,))
        (b,) = c.truth.breaks
        assert b.kind is kind
        s_ids = {x.line_id for x in c.bank_lines}
        l_ids = {x.line_id for x in c.ledger_lines}
        assert set(b.statement_line_ids) <= s_ids and set(b.ledger_line_ids) <= l_ids
        assert b.statement_line_ids or b.ledger_line_ids
        for t in b.treatments:
            for a in t.adjustments:
                assert a.statement_line_ids, "every expected adjustment cites the statement"


def test_break_shapes() -> None:
    def one(kind: BreakKind, seed: int = 3):  # type: ignore[no-untyped-def]
        c = generate_recon_case(seed, 0.5, kinds=(kind,))
        return c, c.truth.breaks[0]

    c, b = one(K.TIMING)
    (lid,) = b.ledger_line_ids
    line = next(x for x in c.ledger_lines if x.line_id == lid)
    assert (c.period_end - line.booked_on).days < TIMING_DAYS
    c, b = one(K.DUPLICATE_LEDGER)
    assert len(b.ledger_line_ids) == 2 and len(b.treatments) == 2
    c, b = one(K.DUPLICATE_BANK)
    assert len(b.statement_line_ids) == 2 and len(b.treatments) == 2
    c, b = one(K.FX)
    assert b.expected.adjustments[0].kind == "fx"
    (lid,) = b.ledger_line_ids
    assert next(x for x in c.ledger_lines if x.line_id == lid).original_currency != c.currency
    c, b = one(K.FEE_NET)
    assert b.expected.adjustments[0].amount_minor < 0
    c, b = one(K.MISSING_IDENTIFIED)
    inv = b.expected.adjustments[0].offset_account.removeprefix("ar:")
    assert inv in {i.invoice_id for i in c.invoices}
    c, b = one(K.PARTIAL_SPLIT)
    assert len(b.statement_line_ids) == 2 and not b.expected.adjustments


def test_unidentified_receipt_can_share_an_open_invoice_amount_at_high_difficulty() -> None:
    hits = 0
    for seed in range(40):
        c = generate_recon_case(seed, 1.0, kinds=(K.MISSING_UNIDENTIFIED,))
        (sid,) = c.truth.breaks[0].statement_line_ids
        amount = next(x.amount_minor for x in c.bank_lines if x.line_id == sid)
        hits += any(i.amount_minor == amount for i in c.invoices)
    assert hits >= 30


def test_views_hide_the_truth() -> None:
    c = generate_recon_case(7, 0.5)
    text = str(c.view()).lower()
    assert "break" not in text and "truth" not in text
    assert not any(k.value in c.case_id for k in BREAK_KINDS)


def test_difficulty_scales_size() -> None:
    small = [len(generate_recon_case(s, 0.0).truth.breaks) for s in range(30)]
    large = [len(generate_recon_case(s, 1.0).truth.breaks) for s in range(30)]
    assert sum(large) > 2 * sum(small)
    with pytest.raises(ValueError, match="difficulty"):
        generate_recon_case(1, -0.1)


# --- engine -----------------------------------------------------------------------------
def test_opening_book_balances_and_untouched_difference() -> None:
    c = generate_recon_case(4, 0.5)
    eng = ReconEngine(c)
    assert eng.book_balance() == c.opening_balance_minor + sum(
        x.amount_minor for x in c.ledger_lines
    )
    assert eng.bank_closing() == c.closing_balance_minor
    st = eng.status()
    assert len(st["unhandled_statement_lines"]) == len(c.bank_lines)  # type: ignore[arg-type]


def test_refusals_change_nothing() -> None:
    c = generate_recon_case(4, 0.5, kinds=(K.FEE_LINE,))
    eng = ReconEngine(c)
    before = eng.state_digest()
    assert eng.act(Match(statement_line_ids=("S-999",), ledger_line_ids=("B-001",))).error is (
        ReconError.UNKNOWN_LINE
    )
    assert eng.act(Unmatch(match_id="M-9")).error is ReconError.UNKNOWN_MATCH
    assert eng.act(Adjust(adjust_kind="fee", amount_minor=0)).error is ReconError.BAD_ADJUSTMENT
    assert eng.act(Adjust(adjust_kind="missing", amount_minor=1, invoice_id="INV-0")).error is (
        ReconError.UNKNOWN_INVOICE
    )
    assert eng.act(Adjust(adjust_kind="duplicate_reversal", amount_minor=1)).error is (
        ReconError.BAD_ADJUSTMENT
    )
    assert eng.act(Escalate()).error is ReconError.EMPTY
    assert eng.state_digest() == before


def test_a_line_can_only_be_handled_once_until_unmatched() -> None:
    c = generate_recon_case(4, 0.5)
    s, lg = c.truth.clean[0]
    eng = ReconEngine(c)
    m = eng.act(Match(statement_line_ids=(s,), ledger_line_ids=(lg,)))
    assert m.ok and m.id == "M-1"
    assert eng.act(Escalate(statement_line_ids=(s,))).error is ReconError.ALREADY_HANDLED
    assert eng.act(MarkTiming(ledger_line_ids=(lg,))).error is ReconError.ALREADY_HANDLED
    assert eng.act(Unmatch(match_id="M-1")).ok
    assert eng.act(Match(statement_line_ids=(s,), ledger_line_ids=(lg,))).id == "M-2"


def test_adjustment_posts_to_the_offset_its_kind_names() -> None:
    c = generate_recon_case(4, 0.5, kinds=(K.FEE_LINE,))
    sid = c.truth.breaks[0].statement_line_ids[0]
    fee = c.truth.breaks[0].expected.adjustments[0].amount_minor
    eng = ReconEngine(c)
    r = eng.act(Adjust(adjust_kind="fee", amount_minor=fee, statement_line_ids=(sid,)))
    assert r.ok and r.id == "ADJ-1"
    assert eng.ledger.balance(FEE_ACCOUNT, c.currency) == -fee
    s = eng.act(Adjust(adjust_kind="missing", amount_minor=100))
    assert s.ok and eng.ledger.balance(SUSPENSE, c.currency) == -100


def test_replay_and_json_round_trip() -> None:
    c = generate_recon_case(9, 0.8)
    eng = ReconEngine(c)
    for s, lg in c.truth.clean:
        eng.act(Match(statement_line_ids=(s,), ledger_line_ids=(lg,)))
    eng.act(Adjust(adjust_kind="fee", amount_minor=-500, statement_line_ids=(c.truth.clean[0][0],)))
    back = ReconEngine.replay(c, ReconEngine.events_from_json(eng.events_json()))
    assert back.state_digest() == eng.state_digest()
    assert back.outcome() == eng.outcome()


# --- properties: ledger balanced; the engine never moves cash except by an adjustment --
_ID = st.integers(1, 40)
_ACTIONS: st.SearchStrategy[ReconAction] = st.one_of(
    st.builds(
        Match,
        statement_line_ids=st.lists(_ID.map(lambda i: f"S-{i:03d}"), min_size=1, max_size=3).map(
            tuple
        ),
        ledger_line_ids=st.lists(_ID.map(lambda i: f"B-{i:03d}"), min_size=1, max_size=3).map(
            tuple
        ),
    ),
    st.builds(Unmatch, match_id=_ID.map(lambda i: f"M-{i}")),
    st.builds(
        Adjust,
        adjust_kind=st.sampled_from(["fee", "fx", "missing", "partial", "duplicate_reversal"]),
        amount_minor=st.integers(-(10**7), 10**7),
        statement_line_ids=st.lists(_ID.map(lambda i: f"S-{i:03d}"), max_size=2).map(tuple),
        ledger_line_ids=st.lists(_ID.map(lambda i: f"B-{i:03d}"), max_size=1).map(tuple),
    ),
    st.builds(
        MarkTiming,
        ledger_line_ids=st.lists(_ID.map(lambda i: f"B-{i:03d}"), min_size=1, max_size=2).map(
            tuple
        ),
    ),
    st.builds(
        Escalate,
        statement_line_ids=st.lists(_ID.map(lambda i: f"S-{i:03d}"), max_size=2).map(tuple),
    ),
)


@given(
    seed=st.integers(0, 5_000),
    difficulty=st.floats(0, 1),
    actions=st.lists(_ACTIONS, max_size=15),
)
def test_property_ledger_balanced_and_cash_moves_only_by_adjustment(
    seed: int, difficulty: float, actions: list[ReconAction]
) -> None:
    c = generate_recon_case(seed, difficulty)
    eng = ReconEngine(c)
    book0 = eng.book_balance()
    for a in actions:
        eng.act(a)
    for e in eng.journal:
        assert sum(p.amount_minor for p in e.postings) == 0
    adj = sum(a.amount_minor for a in eng.outcome().adjustments)
    assert eng.book_balance() == book0 + adj  # matching, timing and escalation move no money
    cash = sum(p.amount_minor for e in eng.journal for p in e.postings if p.account == CASH)
    assert cash == eng.book_balance()
    assert ReconEngine.replay(c, eng.events).state_digest() == eng.state_digest()
