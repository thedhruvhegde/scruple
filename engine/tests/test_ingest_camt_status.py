"""camt.053 entry status: validated against the ISO 20022 code lists, per version.

001.02-06: `<Sts>CODE</Sts>` from EntryStatus2Code; 001.07-13: `<Sts><Cd>` from
ExternalEntryStatus1Code or `<Sts><Prtry>`. Sources in scruple/ingest/bank/camt053.py.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.ingest.bank.camt053 import ENTRY_STATUS_2, EXTERNAL_ENTRY_STATUS, parse_camt053
from scruple.ingest.errors import SchemaMismatchError

OLD = [2, 3, 4, 5, 6]
NEW = [7, 8, 9, 10, 11, 12, 13]


def doc(version: int, sts: str | None) -> bytes:
    status = "" if sts is None else sts
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.{version:02d}">
  <BkToCstmrStmt><Stmt>
    <Id>S-1</Id>
    <Acct><Id><IBAN>DE89370400440532013000</IBAN></Id><Ccy>EUR</Ccy></Acct>
    <Ntry>
      <Amt Ccy="EUR">10.00</Amt>
      <CdtDbtInd>CRDT</CdtDbtInd>
      {status}
      <BookgDt><Dt>2026-02-01</Dt></BookgDt>
    </Ntry>
  </Stmt></BkToCstmrStmt>
</Document>""".encode()


def status_of(data: bytes) -> str | None:
    (s,) = parse_camt053(data)
    (e,) = s.entries
    return e.status


def test_the_code_lists_match_their_sources() -> None:
    assert {"BOOK", "PDNG", "INFO"} == ENTRY_STATUS_2
    assert {"BOOK", "FUTR", "INFO", "PDNG", "RJCT"} == EXTERNAL_ENTRY_STATUS


@pytest.mark.parametrize("version", OLD)
@pytest.mark.parametrize("code", sorted(ENTRY_STATUS_2))
def test_old_versions_take_a_bare_code(version: int, code: str) -> None:
    assert status_of(doc(version, f"<Sts>{code}</Sts>")) == code


@pytest.mark.parametrize("version", NEW)
@pytest.mark.parametrize("code", sorted(EXTERNAL_ENTRY_STATUS))
def test_new_versions_take_an_external_code(version: int, code: str) -> None:
    assert status_of(doc(version, f"<Sts><Cd>{code}</Cd></Sts>")) == code


@pytest.mark.parametrize("version", NEW)
def test_proprietary_status_is_marked_as_such(version: int) -> None:
    assert status_of(doc(version, "<Sts><Prtry>HELD</Prtry></Sts>")) == "prtry:HELD"
    long = status_of(doc(version, "<Sts><Prtry>A-VERY-LONG-BANK-CODE</Prtry></Sts>"))
    assert long is not None and long.startswith("prtry:") and len(long) <= 16


@pytest.mark.parametrize(
    ("version", "sts", "match"),
    [
        (2, "<Sts>FUTR</Sts>", "is not one of"),  # FUTR came with the external list
        (2, "<Sts>RJCT</Sts>", "is not one of"),
        (2, "<Sts>book</Sts>", "is not one of"),
        (2, "<Sts>BOOKED</Sts>", "is not one of"),
        (6, "<Sts><Cd>BOOK</Cd></Sts>", "is a code, not Sts/Cd"),
        (6, "<Sts><Prtry>X</Prtry></Sts>", "is a code, not Sts/Cd"),
        (2, "<Sts></Sts>", "is not one of"),
        (8, "<Sts>BOOK</Sts>", "needs Cd or Prtry"),
        (8, "<Sts><Cd>XXXX</Cd></Sts>", "not an ExternalEntryStatus1Code"),
        (13, "<Sts><Cd>book</Cd></Sts>", "not an ExternalEntryStatus1Code"),
        (8, "<Sts><Cd>BOOK</Cd><Prtry>X</Prtry></Sts>", "exactly one of Cd and Prtry"),
        (8, "<Sts></Sts>", "exactly one of Cd and Prtry"),
    ],
)
def test_unknown_codes_and_wrong_forms_are_refused(version: int, sts: str, match: str) -> None:
    with pytest.raises(SchemaMismatchError, match=match):
        parse_camt053(doc(version, sts))


@pytest.mark.parametrize("version", [2, 8])
def test_a_missing_status_is_no_status(version: int) -> None:
    assert status_of(doc(version, None)) is None


@given(st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=6))
def test_property_accepted_iff_listed(code: str) -> None:
    for version, sts, allowed in (
        (2, f"<Sts>{code}</Sts>", ENTRY_STATUS_2),
        (9, f"<Sts><Cd>{code}</Cd></Sts>", EXTERNAL_ENTRY_STATUS),
    ):
        if code in allowed:
            assert status_of(doc(version, sts)) == code
        else:
            with pytest.raises(SchemaMismatchError):
                parse_camt053(doc(version, sts))
