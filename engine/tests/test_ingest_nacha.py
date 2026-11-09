"""NACHA parser: moov-io/ach public test files, control-record tampering, a generator
of valid files for round-trip properties, and mutation fuzzing."""

from __future__ import annotations

import contextlib
import math
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.ingest.errors import (
    IngestError,
    IntegrityError,
    MalformedInputError,
    UnmappedValueError,
)
from scruple.ingest.nacha import CREDIT_CODES, DEBIT_CODES, parse_nacha
from scruple.ingest.nacha_codes import RETURN_REASONS

FIX = Path(__file__).parent / "fixtures" / "nacha"
WEB = (FIX / "return-WEB.ach").read_bytes()
CUSTOM = (FIX / "return-PPD-custom-reason-code.ach").read_bytes()
PPD = (FIX / "ppd-debit.ach").read_bytes()


def test_return_web_file() -> None:
    f = parse_nacha(WEB)
    assert (f.immediate_destination, f.immediate_origin) == ("091400606", "691000134")
    b1, _ = f.batches
    assert (b1.sec_code, b1.company_name, b1.service_class, b1.batch_number) == (
        "WEB",
        "CoinLion",
        "200",
        1,
    )
    r1, r2 = f.returns()
    assert r1.is_debit and r1.amount_minor == 12354
    assert r1.returned is not None
    assert (r1.returned.reason_code, r1.returned.reason_title) == ("R01", "Insufficient Funds")
    assert r1.returned.original_trace == "091400600000001"
    assert not r2.is_debit
    assert r2.returned is not None
    assert r2.returned.reason_code == "R03"


def test_no_receiver_pii_kept() -> None:
    dump = parse_nacha(WEB).model_dump_json()
    assert "Paul Jones" not in dump
    assert "Bob Marley" not in dump
    assert "MjMxNDAwMjAtOGQ" not in dump  # individual id
    assert parse_nacha(WEB).batches[0].entries[0].account_last4 == "6789"


def test_trimmed_files_need_explicit_opt_in() -> None:
    with pytest.raises(MalformedInputError, match="94"):
        parse_nacha(CUSTOM)
    f = parse_nacha(CUSTOM, allow_trimmed=True)
    (e,) = f.returns()
    assert e.returned is not None
    assert (e.returned.reason_code, e.returned.reason_title) == ("R97", None)
    (p,) = parse_nacha(PPD, allow_trimmed=True).batches
    assert p.entries[0].transaction_code == 27
    assert p.entries[0].returned is None


def _lines() -> list[str]:
    return WEB.decode().splitlines()


def _set(lines: list[str], i: int, a: int, value: str) -> bytes:
    rec = lines[i]
    lines[i] = rec[: a - 1] + value + rec[a - 1 + len(value) :]
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize(
    ("line", "pos", "value", "error"),
    [
        (4, 11, "0009140061", IntegrityError),  # batch entry hash
        (4, 21, "000000012355", IntegrityError),  # batch total debit
        (4, 33, "000000000001", IntegrityError),  # batch total credit
        (4, 5, "000003", IntegrityError),  # batch entry/addenda count
        (4, 88, "0000009", IntegrityError),  # batch number
        (4, 2, "220", IntegrityError),  # service class differs from header
        (9, 2, "000003", IntegrityError),  # file batch count
        (9, 8, "000002", IntegrityError),  # file block count
        (9, 22, "0018280121", IntegrityError),  # file entry hash
        (9, 44, "000000004566", IntegrityError),  # file total credit
        (2, 30, "0000012355", IntegrityError),  # entry amount (both controls disagree)
        (2, 79, "0", IntegrityError),  # addenda indicator off but addenda present
        (3, 80, "091000017611243", IntegrityError),  # addenda trace != entry trace
        (2, 2, "99", UnmappedValueError),  # transaction code
        (3, 2, "97", UnmappedValueError),  # addenda type
        (3, 4, "X01", MalformedInputError),  # reason code shape
        (2, 30, "00000123X4", MalformedInputError),
        (0, 35, "095", MalformedInputError),  # record size
        (1, 2, "999", UnmappedValueError),  # service class
        (1, 70, "991399", MalformedInputError),  # effective date
    ],
)
def test_tampering_is_detected(line: int, pos: int, value: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        parse_nacha(_set(_lines(), line, pos, value))


@pytest.mark.parametrize(
    "body",
    [
        b"",
        WEB.replace(b"\n", b"\n\n", 1),
        WEB + b"9" * 94 + b"\n" + b"1" * 94 + b"\n",
        WEB.replace(b"Paul Jones", "Paul Joné".encode()),
        b"\n".join(WEB.splitlines()[:-1]),  # no file control
        b"\n".join(WEB.splitlines()[1:]),  # no file header
        WEB.splitlines()[0] + b"X\n",
        b"\n".join(WEB.splitlines()[:4] + WEB.splitlines()[3:]),  # duplicated control
    ],
)
def test_structure_errors(body: bytes) -> None:
    with pytest.raises(IngestError):
        parse_nacha(body)


def test_return_reason_table() -> None:
    assert len(RETURN_REASONS) == 71
    assert RETURN_REASONS["R10"].startswith("Customer Advises")
    assert not CREDIT_CODES & DEBIT_CODES


# --- generator of valid files --------------------------------------------------


def _pad(s: str, n: int) -> str:
    assert len(s) <= n
    return s.ljust(n)


def build(entries: list[tuple[int, str, int, str | None]]) -> bytes:
    """entries: (transaction code, 8-digit RDFI, amount, return code or None)."""
    lines = [
        _pad(
            "101 091400606 691000134261109"
            + "0000A094101"
            + "SYNTHETIC DEST         SYNTHETIC ORIGIN       ",
            94,
        )
    ]
    head = (
        "5200"
        + _pad("SYNTH CO", 16)
        + " " * 20
        + "1234567890PPDREFUND    "
        + " " * 6
        + "261109"
        + "   1"
        + "09100001"
        + "0000001"
    )
    lines.append(_pad(head, 94))
    count = hsum = debit = credit = 0
    for i, (code, rdfi, amount, ret) in enumerate(entries):
        trace = f"09100001{i:07d}"
        ind = "1" if ret else "0"
        acct = _pad("ACCT" + str(i), 17)
        name = _pad("SYNTHETIC", 22)
        lines.append(f"6{code:02d}{rdfi}0{acct}{amount:010d}{' ' * 15}{name}  {ind}{trace}")
        count += 1
        if ret:
            lines.append(_pad(f"799{ret}{'0' * 15}      {rdfi}", 79) + trace)
            count += 1
        hsum += int(rdfi)
        if code in DEBIT_CODES:
            debit += amount
        else:
            credit += amount
    ctl = f"8200{count:06d}{hsum % 10**10:010d}{debit:012d}{credit:012d}1234567890"
    lines.append(_pad(ctl, 79) + "091000010000001")
    n = len(lines) + 1
    lines.append(
        _pad(
            f"9000001{math.ceil(n / 10):06d}{count:08d}{hsum % 10**10:010d}"
            f"{debit:012d}{credit:012d}",
            94,
        )
    )
    while len(lines) % 10:
        lines.append("9" * 94)
    assert all(len(x) == 94 for x in lines)
    return ("\n".join(lines) + "\n").encode()


entry_st = st.tuples(
    st.sampled_from(sorted(CREDIT_CODES | DEBIT_CODES)),
    st.integers(min_value=0, max_value=99_999_999).map(lambda x: f"{x:08d}"),
    st.integers(min_value=0, max_value=9_999_999_999),
    st.one_of(st.none(), st.sampled_from(["R01", "R02", "R10", "R29", "R97"])),
)


@settings(max_examples=150, deadline=None)
@given(st.lists(entry_st, max_size=25))
def test_generated_files_round_trip(entries: list[tuple[int, str, int, str | None]]) -> None:
    f = parse_nacha(build(entries))
    (b,) = f.batches
    assert [(e.transaction_code, e.receiving_dfi, e.amount_minor) for e in b.entries] == [
        (c, r, a) for c, r, a, _ in entries
    ]
    assert [e.returned.reason_code if e.returned else None for e in b.entries] == [
        r for *_, r in entries
    ]
    assert sum(e.amount_minor for e in b.entries if e.is_debit) == sum(
        a for c, _, a, _ in entries if c in DEBIT_CODES
    )


def test_generator_matches_hash_truncation() -> None:
    # 200 entries with RDFI 99999999 overflow 10 digits; the rightmost 10 are kept.
    f = parse_nacha(build([(22, "99999999", 1, None)] * 200))
    assert len(f.batches[0].entries) == 200


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=0, max_value=len(WEB) - 1), st.sampled_from(list(b"0123456789R9 X\n")))
def test_fuzz_single_byte_mutations(pos: int, byte: int) -> None:
    mutated = WEB[:pos] + bytes([byte]) + WEB[pos + 1 :]
    with contextlib.suppress(IngestError):
        f = parse_nacha(mutated)
        # Anything accepted still balances.
        assert len(f.batches) == 2


@settings(max_examples=150, deadline=None)
@given(st.binary(max_size=400))
def test_fuzz_bytes(data: bytes) -> None:
    for trimmed in (False, True):
        with contextlib.suppress(IngestError):
            parse_nacha(data, allow_trimmed=trimmed)
