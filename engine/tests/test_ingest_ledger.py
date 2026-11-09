"""Ledger port, in-memory and JSON-file adapters, Formance and Modern Treasury mappings."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.ingest.errors import (
    IngestError,
    MalformedInputError,
    SchemaMismatchError,
    UnmappedValueError,
)
from scruple.ingest.ledger import (
    InMemoryLedger,
    JsonFileLedger,
    LedgerConflictError,
    LedgerLine,
    LedgerPort,
    LedgerTransaction,
    formance_asset,
    from_formance,
    from_journal,
    from_json_doc,
    from_movement,
    to_formance,
    to_json_doc,
    to_modern_treasury,
)
from scruple.ingest.models import LedgerMovement, MovementKind
from scruple.sim.ledger import Ledger

T = datetime(2026, 3, 1, 12, tzinfo=UTC)


def txn(
    ext: str = "t1", lines: list[tuple[str, int]] | None = None, ccy: str = "USD"
) -> LedgerTransaction:
    lines = lines or [("merchant:m1", -1000), ("cardholder:x", 1000)]
    return LedgerTransaction(
        external_id=ext,
        occurred_at=T,
        currency=ccy,
        lines=tuple(LedgerLine(account=a, amount_minor=v) for a, v in lines),
    )


# --- model invariants -----------------------------------------------------------


@pytest.mark.parametrize(
    "lines",
    [
        [("a", -1), ("b", 2)],  # unbalanced
        [("a", 0), ("b", 0)],  # zero lines
        [("a", 5)],  # one line
        [("a:", -1), ("b", 1)],  # empty segment
        [("a b", -1), ("b", 1)],  # space
        [("a/../b", -1), ("b", 1)],
        [("", -1), ("b", 1)],
    ],
)
def test_invalid_transactions(lines: list[tuple[str, int]]) -> None:
    with pytest.raises(ValidationError):
        txn(lines=lines)


def test_naive_datetime_and_unknown_currency_rejected() -> None:
    with pytest.raises(ValidationError):
        LedgerTransaction.model_validate(
            {**txn().model_dump(), "occurred_at": "2026-01-01T00:00:00"}
        )
    with pytest.raises(ValidationError):
        txn(ccy="ZZZ")


# --- port implementations ---------------------------------------------------------


@pytest.fixture(params=["memory", "file"])
def ledger(request: pytest.FixtureRequest, tmp_path: Path) -> LedgerPort:
    if request.param == "memory":
        return InMemoryLedger()
    return JsonFileLedger(tmp_path / "ledger.jsonl")


def test_port_post_is_idempotent_and_conflicts_are_errors(ledger: LedgerPort) -> None:
    assert ledger.post(txn()) is True
    assert ledger.post(txn()) is False
    with pytest.raises(LedgerConflictError):
        ledger.post(txn(lines=[("merchant:m1", -1), ("cardholder:x", 1)]))
    assert ledger.balance("merchant:m1", "USD") == -1000
    assert ledger.balance("merchant:m1", "EUR") == 0
    assert ledger.get("t1") == txn()
    assert ledger.get("nope") is None
    assert len(ledger.transactions()) == 1


def test_json_file_ledger_reloads(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    a = JsonFileLedger(path)
    a.post(txn("t1"))
    a.post(txn("t2", [("x", -5), ("y", 3), ("z", 2)], "JPY"))
    b = JsonFileLedger(path)
    assert b.transactions() == a.transactions()
    assert b.balance("y", "JPY") == 3
    assert len(path.read_text().splitlines()) == 2


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"external_id": "x"}',
        json.dumps(
            {**to_json_doc(txn()), "entries": [{"account": "a", "direction": "debit", "amount": 5}]}
        ),
        json.dumps({**to_json_doc(txn()), "extra": 1}),
        json.dumps(
            {
                **to_json_doc(txn()),
                "entries": [{"account": "a", "direction": "debit", "amount": True}] * 2,
            }
        ),
    ],
)
def test_json_file_ledger_rejects_corrupt_files(tmp_path: Path, line: str) -> None:
    path = tmp_path / "l.jsonl"
    path.write_text(json.dumps(to_json_doc(txn())) + "\n" + line + "\n")
    with pytest.raises(MalformedInputError, match="line 2"):
        JsonFileLedger(path)


def test_generic_json_doc_shape() -> None:
    doc = to_json_doc(txn())
    assert doc["entries"] == [
        {"account": "merchant:m1", "direction": "credit", "amount": 1000},
        {"account": "cardholder:x", "direction": "debit", "amount": 1000},
    ]
    assert from_json_doc(doc) == txn()


# --- conversions ------------------------------------------------------------------


def test_from_movement_balances_and_matches_sim_ledger() -> None:
    m = LedgerMovement(
        source="stripe",
        external_id="txn_1",
        case_external_id="dp_1",
        payment_ref="ch_1",
        kind=MovementKind.DISPUTE_DEBIT,
        amount_minor=-1000,
        fee_minor=1500,
        net_minor=-2500,
        currency="USD",
        occurred_at=T,
        category="dispute",
    )
    port = InMemoryLedger()
    sim = Ledger()
    for t in from_movement(m, "merchant:acct_1"):
        port.post(t)
    for kind, ref, ccy, postings in m.journal_postings("merchant:acct_1"):
        sim.post(kind, ref, "dp_1", ccy, postings)
    for acct in ("merchant:acct_1", "processor:disputes", "processor:fees"):
        assert port.balance(acct, "USD") == sim.balance(acct, "USD")
    assert port.balance("merchant:acct_1", "USD") == m.net_minor


def test_from_journal() -> None:
    sim = Ledger()
    e = sim.post("capture", "txn-1", None, "EUR", [("cardholder:txn-1", -500), ("merchant:m", 500)])
    t = from_journal(e, T)
    assert t.external_id == "sim:1"
    assert t.net_by_account() == {"cardholder:txn-1": -500, "merchant:m": 500}
    assert t.metadata == {"kind": "capture", "txn": "txn-1"}


def test_formance_shape() -> None:
    body = to_formance(txn("t9", [("world", -300), ("a", 100), ("b", 200)]))
    assert body["reference"] == "t9"
    assert body["timestamp"] == T.isoformat()
    for p in body["postings"]:
        assert set(p) == {"source", "destination", "amount", "asset"}  # PostingRequest
        assert p["asset"] == "USD/2"
        assert p["amount"] > 0
    assert sum(p["amount"] for p in body["postings"]) == 300
    assert formance_asset("JPY") == "JPY/0"
    assert formance_asset("KWD") == "KWD/3"


@pytest.mark.parametrize(
    "posting",
    [
        {"source": "a", "destination": "b", "amount": 1, "asset": "USD"},
        {"source": "a", "destination": "b", "amount": 1, "asset": "USD/3"},
        {"source": "a", "destination": "b", "amount": -1, "asset": "USD/2"},
        {"source": "a", "destination": "b", "amount": True, "asset": "USD/2"},
        {"source": "a", "destination": "b", "amount": "1.5", "asset": "USD/2"},
        {"source": "a:", "destination": "b", "amount": 1, "asset": "USD/2"},
        {"source": "a", "destination": "b", "amount": 1, "asset": "XYZ/2"},
        "not an object",
    ],
)
def test_from_formance_rejects(posting: Any) -> None:
    with pytest.raises(IngestError):
        from_formance({"postings": [posting], "reference": "r", "timestamp": T.isoformat()})


def test_from_formance_bigint_string_and_multi_asset() -> None:
    body = {
        "postings": [
            {"source": "a", "destination": "b", "amount": "12345678901234567890", "asset": "USD/2"}
        ],
        "reference": "r",
        "timestamp": T.isoformat(),
    }
    assert from_formance(body).net_by_account()["b"] == 12345678901234567890
    body["postings"].append({"source": "a", "destination": "b", "amount": 1, "asset": "EUR/2"})  # type: ignore[attr-defined]
    with pytest.raises(SchemaMismatchError, match="multi-asset"):
        from_formance(body)
    with pytest.raises(SchemaMismatchError):
        from_formance({"postings": []})


def test_modern_treasury_stub() -> None:
    ids = {"merchant:m1": "mt-acct-1", "cardholder:x": "mt-acct-2"}
    body = to_modern_treasury(txn(), ids)
    assert body["ledger_entries"] == [
        {"direction": "credit", "ledger_account_id": "mt-acct-1", "amount": 1000},
        {"direction": "debit", "ledger_account_id": "mt-acct-2", "amount": 1000},
    ]
    assert body["status"] == "posted"
    assert body["external_id"] == "t1"
    debits = sum(e["amount"] for e in body["ledger_entries"] if e["direction"] == "debit")
    credits = sum(e["amount"] for e in body["ledger_entries"] if e["direction"] == "credit")
    assert debits == credits
    with pytest.raises(UnmappedValueError):
        to_modern_treasury(txn(), {"merchant:m1": "x"})


# --- properties -------------------------------------------------------------------

accounts = st.sampled_from(["a", "b", "c:d", "e-f", "g_h:i"])


@st.composite
def balanced(draw: st.DrawFn) -> LedgerTransaction:
    n = draw(st.integers(min_value=1, max_value=6))
    amts = [
        draw(st.integers(min_value=1, max_value=10**12)) * draw(st.sampled_from([1, -1]))
        for _ in range(n)
    ]
    lines = [(draw(accounts), a) for a in amts]
    lines.append((draw(accounts), -sum(amts)) if sum(amts) else (draw(accounts), 0))
    lines = [(acc, v) for acc, v in lines if v]
    if len(lines) < 2 or len({a for a, _ in lines}) < 2:
        lines = [("a", -7), ("b", 7)]
    return txn("p", lines, draw(st.sampled_from(["USD", "JPY", "KWD"])))


@settings(max_examples=200, deadline=None)
@given(balanced())
def test_round_trips_preserve_net_positions(t: LedgerTransaction) -> None:
    assert from_json_doc(json.loads(json.dumps(to_json_doc(t)))) == t
    back = from_formance(to_formance(t)) if t.net_by_account() else None
    if back is not None:
        assert back.net_by_account() == t.net_by_account()
        assert back.currency == t.currency


@settings(max_examples=200, deadline=None)
@given(
    st.dictionaries(
        st.text(max_size=12),
        st.one_of(
            st.none(), st.integers(), st.text(max_size=10), st.lists(st.integers(), max_size=2)
        ),
        max_size=6,
    )
)
def test_fuzz_parsers(doc: dict[str, Any]) -> None:
    with contextlib.suppress(IngestError):
        from_json_doc(doc)
    with contextlib.suppress(IngestError):
        from_formance(doc)
