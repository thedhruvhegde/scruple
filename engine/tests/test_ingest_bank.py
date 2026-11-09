"""Bank statements: BAI2 against real public samples, camt.053 (real header sample plus
a hand-made statement), XML attacks, bank CSV, and fuzzing of every parser."""

from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.ingest.bank import (
    BankCsvConfig,
    Direction,
    import_bank_csv,
    parse_bai2,
    parse_camt053,
)
from scruple.ingest.bank.bai2_codes import CREDIT_CODES, DEBIT_CODES, MISC_CODES, STATUS_CODES
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    IntegrityError,
    MalformedInputError,
    SchemaMismatchError,
    UnmappedValueError,
)

FIX = Path(__file__).parent / "fixtures" / "bank"
SVB = (FIX / "svb_us_example.bai2").read_text()
CITI = (FIX / "citi_example.bai2").read_text()
NWB = (FIX / "nwb_example.bai2").read_text()
BLANK = (FIX / "account_trailer_amount_blank_example.bai2").read_text()
HANDMADE = (FIX / "handmade_camt.053.001.02.xml").read_bytes()
PROWIDE = (FIX / "prowide_camt.053.001.07.xml").read_bytes()


# --- BAI2: real files from ministryofjustice/bai2 -------------------------------


def test_svb_sample() -> None:
    a, b = parse_bai2(SVB)
    assert (a.account_id, a.currency) == ("1234567890", "USD")
    assert {x.kind: x.amount_minor for x in a.balances}["010"] == 34726979
    assert {x.kind: x.amount_minor for x in a.balances}["015"] == 35217175
    (e,) = a.entries
    assert (e.type_code, e.direction, e.amount_minor) == ("142", Direction.CREDIT, 490196)
    assert e.bank_ref == "172629"
    assert e.description == "SOME PAYMENT ACH OFFSET"
    assert e.booking_date == date(2022, 2, 1)
    assert b.entries[0].amount_minor == 905800


def test_citi_sample_value_dated_funds_and_long_text() -> None:
    (s,) = parse_bai2(CITI)
    assert s.currency == "GBP"
    (e,) = s.entries
    assert (e.type_code, e.amount_minor, e.bank_ref, e.customer_ref) == (
        "191",
        1,
        "1234567890",
        "RP12312312312312",
    )
    assert e.description is not None
    assert e.description.startswith("FR:FP SIP INCOMING ENDT:20150715")


def test_nwb_sample_group_without_currency_uses_account_currency() -> None:
    (s,) = parse_bai2(NWB)
    assert s.currency == "GBP"
    assert [e.direction for e in s.entries].count(Direction.DEBIT) == 4
    assert s.net_minor() == -971 - 100 - 123 - 1571 + 89


def test_blank_trailer_sample_needs_explicit_currency() -> None:
    with pytest.raises(MalformedInputError, match="default_currency"):
        parse_bai2(BLANK)
    assert len(parse_bai2(BLANK, default_currency="USD")) == 2


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        ("49,138884258,7/", "49,138884259,7/", IntegrityError),  # account total
        ("49,138884258,7/", "49,138884258,8/", IntegrityError),  # account record count
        ("98,138884258,1,9/", "98,138884258,2,9/", IntegrityError),  # number of accounts
        ("99,282030196,2,20", "99,282030196,2,21", IntegrityError),  # file record count
        ("99,282030196,2,20", "99,282030197,2,20", IntegrityError),  # file total
        ("16,142,490196,,172629,/", "16,142,490197,,172629,/", IntegrityError),
        ("16,142,490196", "16,999,490196", UnmappedValueError),  # unknown type code
        ("16,142,490196", "16,010,490196", MalformedInputError),  # status code as a txn
        ("16,142,490196", "16,142,4x0196", MalformedInputError),
        (
            "01,121140399,121140399,230214,0754,1,,,2/",
            "01,1,1,230214,0754,1,,,3/",
            MalformedInputError,
        ),
        ("02,121140399,121140399,1,220201", "02,121140399,121140399,1,221301", MalformedInputError),
        ("03,1234567890,USD", "03,1234567890,ZZZ", UnmappedValueError),
    ],
)
def test_bai2_tampering_detected(old: str, new: str, error: type[Exception]) -> None:
    assert old in SVB
    with pytest.raises(error):
        parse_bai2(SVB.replace(old, new, 1))


def test_bai2_integrity_can_be_relaxed_explicitly() -> None:
    assert parse_bai2(SVB.replace("49,138884258,7/", "49,1,7/", 1), check_integrity=False)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "01,a/",
        "88,orphan",
        "XX,1/",
        SVB.split("99,")[0],  # no file trailer
        SVB + "01,again/\n",
        SVB.replace("16,142,490196,,172629,/", "16,142/", 1),
        "01,a,b,230101,0000,1,,,2/\n99,0,0,2/",
        "\x00",
        "01," + "x" * 5000,
    ],
)
def test_bai2_structure_errors(text: str) -> None:
    with pytest.raises(IngestError):
        parse_bai2(text)


def test_bai2_code_table_is_disjoint_and_complete() -> None:
    sets = [STATUS_CODES, CREDIT_CODES, DEBIT_CODES, MISC_CODES]
    assert sum(len(s) for s in sets) == len(set().union(*sets)) == 484
    assert {"010", "015", "040", "045"} <= STATUS_CODES
    assert "142" in CREDIT_CODES
    assert "699" in DEBIT_CODES


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet="0123456789,/+- ABCDVSZ\n", max_size=300))
def test_fuzz_bai2(text: str) -> None:
    with contextlib.suppress(IngestError):
        parse_bai2("01,a,b,230101,0000,1,,,2/\n" + text, default_currency="USD")


@settings(max_examples=100, deadline=None)
@given(st.integers(min_value=0, max_value=len(SVB) - 1), st.sampled_from(list(",/0129\n88")))
def test_fuzz_bai2_single_char_mutations(pos: int, ch: str) -> None:
    mutated = SVB[:pos] + ch + SVB[pos + 1 :]
    try:
        stmts = parse_bai2(mutated)
    except IngestError:
        return
    # Accepted mutations must still satisfy the control totals that were checked.
    assert stmts


# --- camt.053 -------------------------------------------------------------------


def test_handmade_camt053() -> None:
    (s,) = parse_camt053(HANDMADE)
    assert (s.statement_id, s.account_id, s.currency) == (
        "SYNTH-STMT-1",
        "DE00SYNTHETIC0000000001",
        "EUR",
    )
    bal = {b.kind: b.amount_minor for b in s.balances}
    assert bal == {"OPBD": 100000, "CLBD": 117025}
    assert bal["OPBD"] + s.net_minor() == bal["CLBD"]
    e1, e2, e3 = s.entries
    assert (e1.direction, e1.amount_minor, e1.type_code) == (
        Direction.CREDIT,
        25000,
        "PMNT/RCDT/ESCT",
    )
    assert (e1.end_to_end_id, e1.bank_ref, e1.customer_ref, e1.status) == (
        "E2E-0001",
        "BANKREF-1",
        "E1",
        "BOOK",
    )
    assert e1.description == "Settlement batch 0001"
    assert e2.description == "Chargeback debit, case SYNTH-42"
    assert (e3.return_reason, e3.type_code, e3.booking_date) == ("AC04", "RETURN", date(2026, 2, 1))


def test_prowide_public_sample_with_app_header() -> None:
    (s,) = parse_camt053(PROWIDE)
    assert (s.account_id, s.currency) == ("2345234534", "USD")
    assert {b.kind: b.amount_minor for b in s.balances} == {"OPAV": 23423400, "ITBD": 23429900}
    assert s.entries == ()


BOMB = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><x>&lol2;</x></Document>"""
XXE = b"""<?xml version="1.0"?>
<!DOCTYPE d [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><x>&xxe;</x></Document>"""
DTD_ONLY = b"""<?xml version="1.0"?><!DOCTYPE d SYSTEM "http://example.invalid/x.dtd">
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"/>"""


@pytest.mark.parametrize("payload", [BOMB, XXE, DTD_ONLY])
def test_xml_attacks_refused(payload: bytes) -> None:
    with pytest.raises(MalformedInputError, match="unsafe XML"):
        parse_camt053(payload)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"xsd:camt.053.001.02", b"xsd:camt.052.001.02"),
        (b'<Amt Ccy="EUR">250.00</Amt>', b'<Amt Ccy="EUR">250.001</Amt>'),
        (b'<Amt Ccy="EUR">250.00</Amt>', b'<Amt Ccy="USD">250.00</Amt>'),
        (b'<Amt Ccy="EUR">250.00</Amt>', b'<Amt Ccy="EUR">-250.00</Amt>'),
        (b'<Amt Ccy="EUR">250.00</Amt>', b"<Amt>250.00</Amt>"),
        (b"<CdtDbtInd>CRDT</CdtDbtInd>\n        <Sts>", b"<CdtDbtInd>X</CdtDbtInd>\n        <Sts>"),
        (b"<IBAN>DE00SYNTHETIC0000000001</IBAN>", b""),
        (b"<Dt>2026-02-01</Dt></BookgDt>", b"<Dt>yesterday</Dt></BookgDt>"),
        (b"</Document>", b""),
    ],
)
def test_camt053_rejects(old: bytes, new: bytes) -> None:
    assert old in HANDMADE
    with pytest.raises(IngestError):
        parse_camt053(HANDMADE.replace(old, new, 1))


def test_camt053_size_limit() -> None:
    with pytest.raises(InputTooLargeError):
        parse_camt053(HANDMADE, max_bytes=100)


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=300))
def test_fuzz_camt053_bytes(data: bytes) -> None:
    with contextlib.suppress(IngestError):
        parse_camt053(data)


@settings(max_examples=150, deadline=None)
@given(st.integers(min_value=0, max_value=len(HANDMADE) - 1), st.binary(min_size=1, max_size=3))
def test_fuzz_camt053_mutations(pos: int, chunk: bytes) -> None:
    with contextlib.suppress(IngestError):
        parse_camt053(HANDMADE[:pos] + chunk + HANDMADE[pos + 1 :])


# --- bank CSV -------------------------------------------------------------------

SIGNED = BankCsvConfig(
    account_id="acct-1",
    currency="usd",
    booking_date="date",
    amount="amount",
    reference="ref",
    description="memo",
)
SPLIT = BankCsvConfig(
    account_id="acct-1",
    currency="EUR",
    booking_date="Date",
    debit="Out",
    credit="In",
    date_format="dmy",
    delimiter=";",
)


def test_bank_csv_signed() -> None:
    body = (
        "date,amount,ref,memo\n2026-01-02,-12.50,R1,Chargeback\n"
        "2026-01-03,100,R2,Settlement\n2026-01-04,0,R3,x\nbad,1,R4,y\n"
    )
    rep = import_bank_csv(body, SIGNED)
    assert rep.rows_read == 4
    assert [e.signed_minor for e in rep.statement.entries] == [-1250, 10000]
    assert [e.row for e in rep.errors] == [3, 4]
    assert rep.statement.currency == "USD"


def test_bank_csv_split_columns() -> None:
    body = (
        "Date;Out;In\n02/01/2026;12,50;\n03/01/2026;;5.00\n"
        "04/01/2026;1.00;2.00\n05/01/2026;-1.00;\n"
    )
    rep = import_bank_csv(body, SPLIT)
    assert [e.signed_minor for e in rep.statement.entries] == [500]
    assert rep.statement.entries[0].booking_date == date(2026, 1, 3)
    assert [e.row for e in rep.errors] == [1, 3, 4]  # "12,50" is not a plain decimal


def test_bank_csv_config_rules() -> None:
    with pytest.raises(ValidationError):
        BankCsvConfig(account_id="a", currency="USD", booking_date="d")
    with pytest.raises(ValidationError):
        BankCsvConfig(account_id="a", currency="USD", booking_date="d", amount="x", debit="y")
    with pytest.raises(SchemaMismatchError):
        import_bank_csv("x,y\n", SIGNED)


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=300))
def test_fuzz_bank_csv(text: str) -> None:
    with contextlib.suppress(IngestError):
        rep = import_bank_csv("date,amount,ref,memo\n" + text, SIGNED)
        assert len(rep.statement.entries) + len({e.row for e in rep.errors}) == rep.rows_read
