"""CSV and JSONL case import: strict validation, row-level reports, no silent drops."""

from __future__ import annotations

import contextlib
import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.domain import ReasonCode
from scruple.ingest.currency import decimal_to_minor, minor_to_decimal
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    MalformedInputError,
    SchemaMismatchError,
    UnmappedValueError,
)
from scruple.ingest.models import CaseState
from scruple.ingest.tabular import (
    ColumnMapping,
    ImportConfig,
    ImportFailedError,
    StatusTarget,
    import_csv,
    import_jsonl,
)
from scruple.sim.models import Stage

FIXTURE = (Path(__file__).parent / "fixtures" / "tabular" / "cases.csv").read_bytes()

COLUMNS = ColumnMapping(
    external_id="case_id",
    payment_ref="charge",
    amount="amount",
    currency="ccy",
    status="state",
    reason="why",
    opened_at="opened",
    evidence_due_at="due",
    network="scheme",
    network_reason_code="scheme_code",
)
CFG = ImportConfig(
    columns=COLUMNS,
    status_map={
        "open": StatusTarget(stage=Stage.CHARGEBACK, state=CaseState.OPEN),
        "won": StatusTarget(stage=Stage.RESOLVED, state=CaseState.WON),
        "lost": StatusTarget(stage=Stage.RESOLVED, state=CaseState.LOST),
    },
    reason_map={"fraud": ReasonCode.FRAUD, "item_missing": ReasonCode.NOT_RECEIVED, "other": None},
)
HEADER = "case_id,charge,amount,ccy,state,why,opened,due,scheme,scheme_code\n"


def test_fixture_report() -> None:
    rep = import_csv(FIXTURE, CFG)
    assert rep.rows_read == 7
    assert [c.external_id for c in rep.cases] == ["c-001", "c-002", "c-003"]
    assert rep.rows_failed == 4
    by_row = {e.row: e for e in rep.errors}
    assert set(by_row) == {4, 5, 6, 7}
    assert by_row[4].column == "amount"
    assert by_row[5].column == "state"
    assert by_row[6].column == "case_id"
    assert "row 1" in by_row[6].message
    assert by_row[7].column == "opened"
    c1, c2, c3 = rep.cases
    assert (c1.amount_minor, c1.currency, c1.reason_code) == (1250, "USD", ReasonCode.FRAUD)
    assert c1.evidence_due_at is not None
    assert (c1.network, c1.network_reason_code) == ("visa", "10.4")
    assert c2.network_reason_code is None
    assert (c3.amount_minor, c3.currency, c3.reason_code) == (1000, "JPY", None)
    assert c3.opened_at.utcoffset() is not None
    assert c1.case_id == "csv:c-001"


def test_strict_raises_with_report() -> None:
    with pytest.raises(ImportFailedError) as e:
        import_csv(FIXTURE, CFG, strict=True)
    assert e.value.report.rows_failed == 4
    good = HEADER + "a,p,1.00,USD,open,fraud,2026-01-01,,,\n"
    assert len(import_csv(good, CFG, strict=True).cases) == 1


def test_minor_amount_format() -> None:
    cfg = CFG.model_copy(update={"amount_format": "minor"})
    rep = import_csv(
        HEADER + "a,p,1250,USD,open,fraud,2026-01-01,,,\nb,p,1.5,USD,open,fraud,2026-01-01,,,\n",
        cfg,
    )
    assert rep.cases[0].amount_minor == 1250
    assert rep.errors[0].row == 2


@pytest.mark.parametrize(
    ("header", "msg"),
    [
        ("case_id,charge,amount\n", "missing"),
        (HEADER.strip() + ",secret_notes\n", "unmapped"),
        (HEADER.strip() + ",case_id\n", "duplicate"),
        ("", "empty"),
    ],
)
def test_header_problems_are_file_level(header: str, msg: str) -> None:
    with pytest.raises(SchemaMismatchError, match=msg):
        import_csv(header, CFG)


def test_extra_columns_allowed_when_declared() -> None:
    cfg = CFG.model_copy(update={"allow_extra_columns": True})
    rep = import_csv(HEADER.strip() + ",notes\na,p,1.00,USD,open,fraud,2026-01-01,,,,hi\n", cfg)
    assert len(rep.cases) == 1


def test_wrong_field_count_and_bad_quoting() -> None:
    rep = import_csv(HEADER + "a,p,1.00\nb,p,1.00,USD,open,fraud,2026-01-01,,,\n", CFG)
    assert [e.row for e in rep.errors] == [1]
    assert len(rep.cases) == 1
    rep = import_csv(HEADER + 'a,"p,1.00,USD,open,fraud,2026-01-01,,,\n', CFG)
    assert rep.errors and rep.cases == ()


def test_hostile_files() -> None:
    with pytest.raises(MalformedInputError):
        import_csv(b"\xff\xfe" + HEADER.encode(), CFG)
    with pytest.raises(MalformedInputError):
        import_csv(HEADER + "a\x00,p\n", CFG)
    with pytest.raises(InputTooLargeError):
        import_csv(HEADER * 10, CFG.model_copy(update={"max_bytes": 100}))
    many = HEADER + "a,p,1.00,USD,open,fraud,2026-01-01,,,\n" * 5
    with pytest.raises(InputTooLargeError):
        import_csv(many, CFG.model_copy(update={"max_rows": 3}))
    long = HEADER + "a" * 5000 + ",p,1.00,USD,open,fraud,2026-01-01,,,\n"
    assert import_csv(long, CFG).errors[0].message.startswith("column")
    # A formula-looking value is just a string; nothing is evaluated.
    rep = import_csv(HEADER + "=1+1,@SUM(A1),1.00,USD,open,fraud,2026-01-01,,,\n", CFG)
    assert rep.cases[0].external_id == "=1+1"


def test_bom_and_semicolons() -> None:
    cfg = CFG.model_copy(update={"delimiter": ";"})
    body = "﻿" + HEADER.replace(",", ";") + "a;p;1.00;USD;open;fraud;2026-01-01;;;\n"
    assert len(import_csv(body.encode(), cfg).cases) == 1


def test_config_validation() -> None:
    with pytest.raises(ValidationError):
        ColumnMapping(
            external_id="x",
            payment_ref="x",
            amount="a",
            currency="c",
            status="s",
            reason="r",
            opened_at="o",
        )
    with pytest.raises(ValidationError):
        ImportConfig(columns=COLUMNS, status_map={}, reason_map={"a": None})
    with pytest.raises(ValidationError):
        ImportConfig.model_validate({**CFG.model_dump(), "surprise": 1})


# --- JSONL ----------------------------------------------------------------------


def jsonl(rows: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(r) for r in rows)


ROW: dict[str, Any] = {
    "case_id": "a",
    "charge": "p",
    "amount": "1.00",
    "ccy": "USD",
    "state": "open",
    "why": "fraud",
    "opened": "2026-01-01",
    "due": None,
    "scheme": None,
    "scheme_code": None,
}


def test_jsonl_import() -> None:
    rows: list[dict[str, Any]] = [
        ROW,
        dict(ROW, case_id="b", amount=5),
        dict(ROW, case_id="c", ccy=["USD"]),
        dict(ROW, case_id="d", x=1),
    ]
    rep = import_jsonl(jsonl(rows) + "\nnot json", CFG)
    assert rep.rows_read == 5
    assert [c.external_id for c in rep.cases] == ["a"]
    assert {e.row for e in rep.errors} == {2, 3, 4, 5}
    minor = CFG.model_copy(update={"amount_format": "minor"})
    assert import_jsonl(jsonl([dict(ROW, amount=150)]), minor).cases[0].amount_minor == 150
    with pytest.raises(SchemaMismatchError):
        import_jsonl("", CFG)
    with pytest.raises(SchemaMismatchError):
        import_jsonl("[1]\n2", CFG)


# --- properties -------------------------------------------------------------------

good_or_bad = st.fixed_dictionaries(
    {
        "case_id": st.sampled_from(["a", "b", "c", "", "x" * 300]),
        "charge": st.sampled_from(["p1", ""]),
        "amount": st.sampled_from(
            ["1.00", "0", "12.345", "-1", "1e3", "abc", "99999999999999999999"]
        ),
        "ccy": st.sampled_from(["USD", "jpy", "XXX", "", "US"]),
        "state": st.sampled_from(["open", "won", "nope"]),
        "why": st.sampled_from(["fraud", "other", "?"]),
        "opened": st.sampled_from(["2026-01-01", "2026-01-01T00:00:00Z", "2026-13-01", "now", ""]),
        "due": st.sampled_from(["", "2026-02-01", "bad"]),
        "scheme": st.sampled_from(["", "visa"]),
        "scheme_code": st.sampled_from(["", "10.4"]),
    }
)


@settings(max_examples=150, deadline=None)
@given(st.lists(good_or_bad, max_size=12))
def test_no_row_is_silently_dropped(rows: list[dict[str, str]]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(ROW))
    w.writeheader()
    w.writerows(rows)
    rep = import_csv(buf.getvalue(), CFG)
    failed = {e.row for e in rep.errors}
    assert rep.rows_read == len(rows)
    assert len(rep.cases) + len(failed) == len(rows)
    assert len({c.external_id for c in rep.cases}) == len(rep.cases)


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=10**12), st.sampled_from(["USD", "JPY", "KWD"])
        ),
        min_size=1,
        max_size=10,
    )
)
def test_round_trip_through_csv(items: list[tuple[int, str]]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(ROW))
    w.writeheader()
    for i, (amt, ccy) in enumerate(items):
        w.writerow(dict(ROW, case_id=f"c{i}", amount=minor_to_decimal(amt, ccy), ccy=ccy, due=""))
    rep = import_csv(buf.getvalue(), CFG, strict=True)
    assert [(c.amount_minor, c.currency) for c in rep.cases] == items


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=400))
def test_fuzz_csv_bytes(data: bytes) -> None:
    with contextlib.suppress(IngestError):
        rep = import_csv(HEADER.encode() + data, CFG)
        assert len(rep.cases) <= rep.rows_read


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300))
def test_fuzz_jsonl_text(data: str) -> None:
    with contextlib.suppress(IngestError):
        import_jsonl(data, CFG)


@pytest.mark.parametrize(
    ("text", "ccy", "minor"),
    [
        ("12.34", "USD", 1234),
        ("-0.5", "EUR", -50),
        ("100", "JPY", 100),
        ("1.234", "KWD", 1234),
        ("+7", "USD", 700),
    ],
)
def test_decimal_to_minor(text: str, ccy: str, minor: int) -> None:
    assert decimal_to_minor(text, ccy) == minor


@pytest.mark.parametrize(
    "text", ["", "1,000.00", "1e3", "NaN", "Infinity", "1.001", "--1", "1.2.3", " ", "٣"]
)
def test_decimal_to_minor_rejects(text: str) -> None:
    with pytest.raises(SchemaMismatchError):
        decimal_to_minor(text, "USD")


def test_unknown_currency() -> None:
    with pytest.raises(UnmappedValueError):
        decimal_to_minor("1", "XYZ")
