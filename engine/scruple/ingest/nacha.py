"""NACHA (ACH) file parser, focused on returns.

Record positions and control rules follow moov-io/ach (Apache 2.0) at c7025ec
(`fileHeader.go`, `batchHeader.go`, `entryDetail.go`, `addenda99.go`,
`batchControl.go`, `fileControl.go`) and were checked against its public test files
(vendored in tests/fixtures/nacha/):

- fixed 94-character ASCII records: 1 file header, 5 batch header, 6 entry detail,
  7 addenda, 8 batch control, 9 file control, then optional all-"9" filler lines
- file header: priority code "01", record size "094", blocking factor "10", format "1"
- entry: transaction code (2), receiving DFI id (8), check digit (1), DFI account (17),
  amount in cents (10), individual id (15), name (22), discretionary (2), addenda
  indicator (1), trace number (15)
- return addenda: type "99", reason code (3, e.g. R01), original trace (15), date of
  death (6), original receiving DFI (8), addenda information (44), trace number (15)
- batch control: service class, entry/addenda count, entry hash (sum of the 8-digit
  receiving DFI ids, rightmost 10 digits), total debit and total credit amounts,
  batch number; file control: batch count, block count (records / 10, rounded up),
  entry/addenda count, entry hash, total debit and credit
- transaction codes: 21-24, 31-34, 41-44, 51-54 credit; 26-29, 36-39, 46-49, 55, 56
  debit (moov `entryDetail.go` constants)

Privacy: the receiver's account number is reduced to its last four characters and the
receiver name and individual id are not kept.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from scruple.ingest.errors import (
    InputTooLargeError,
    IntegrityError,
    MalformedInputError,
    SchemaMismatchError,
    UnmappedValueError,
)
from scruple.ingest.limits import MAX_FILE_BYTES, check_size
from scruple.ingest.nacha_codes import RETURN_REASONS

RECORD_LEN = 94
CREDIT_CODES = frozenset({21, 22, 23, 24, 31, 32, 33, 34, 41, 42, 43, 44, 51, 52, 53, 54})
DEBIT_CODES = frozenset({26, 27, 28, 29, 36, 37, 38, 39, 46, 47, 48, 49, 55, 56})
MAX_ADDENDA = 9999


class AchReturn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason_code: str = Field(pattern=r"^R[0-9A-Z]{2}$")
    reason_title: str | None
    original_trace: str
    original_rdfi: str
    trace_number: str


class AchEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_code: int
    is_debit: bool
    receiving_dfi: str
    account_last4: str
    amount_minor: int = Field(ge=0)
    trace_number: str
    addenda_types: tuple[str, ...] = ()
    returned: AchReturn | None = None


class AchBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_number: int
    service_class: str
    company_name: str
    company_id: str
    sec_code: str
    entry_description: str
    effective_date: date | None
    odfi: str
    entries: tuple[AchEntry, ...]


class AchFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    immediate_destination: str
    immediate_origin: str
    creation_date: date | None
    batches: tuple[AchBatch, ...]

    def returns(self) -> tuple[AchEntry, ...]:
        return tuple(e for b in self.batches for e in b.entries if e.returned is not None)


def _err(line: int, msg: str) -> MalformedInputError:
    return MalformedInputError(f"NACHA line {line}: {msg}")


def _num(rec: str, a: int, b: int, line: int, what: str) -> int:
    """1-based inclusive positions a..b, digits only (spaces not allowed)."""
    s = rec[a - 1 : b]
    if not s.isdigit():
        raise _err(line, f"{what} (positions {a}-{b}) is not numeric: {s!r}")
    return int(s)


def _txt(rec: str, a: int, b: int) -> str:
    return rec[a - 1 : b].strip()


def _yymmdd(s: str, line: int) -> date | None:
    if not s.strip():
        return None
    try:
        return datetime.strptime(s, "%y%m%d").date()
    except ValueError:
        raise _err(line, f"date {s!r} is not YYMMDD") from None


def _lines(data: bytes, allow_trimmed: bool) -> list[str]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as e:
        raise MalformedInputError(f"NACHA file is not ASCII (byte {e.start})") from None
    out = []
    for n, raw in enumerate(text.splitlines(), start=1):
        if len(raw) != RECORD_LEN:
            if allow_trimmed and len(raw) < RECORD_LEN and raw.rstrip(" ") == raw:
                raw = raw.ljust(RECORD_LEN)
            else:
                raise _err(n, f"record is {len(raw)} characters, not {RECORD_LEN}")
        if not raw.strip():
            raise _err(n, "blank record")
        out.append(raw)
    return out


class _Cursor:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.i = 0

    def peek(self) -> str | None:
        return self.lines[self.i][0] if self.i < len(self.lines) else None

    def take(self, kind: str) -> tuple[str, int]:
        if self.i >= len(self.lines):
            raise MalformedInputError(f"NACHA file ended early, expected record type {kind}")
        rec = self.lines[self.i]
        if rec[0] != kind:
            raise _err(self.i + 1, f"expected record type {kind}, got {rec[0]!r}")
        self.i += 1
        return rec, self.i


def _addenda(c: _Cursor, entry_trace: str) -> Iterator[tuple[str, AchReturn | None]]:
    count = 0
    while c.peek() == "7":
        rec, n = c.take("7")
        count += 1
        if count > MAX_ADDENDA:
            raise InputTooLargeError("too many addenda records for one entry")
        kind = rec[1:3]
        if kind == "99":
            code = rec[3:6]
            if not (code[0] == "R" and code[1:].isalnum()):
                raise _err(n, f"return reason code {code!r} is not R followed by two characters")
            trace = rec[79:94]
            if trace != entry_trace:
                raise IntegrityError(f"NACHA line {n}: addenda trace does not match its entry")
            yield (
                kind,
                AchReturn(
                    reason_code=code,
                    reason_title=RETURN_REASONS.get(code),
                    original_trace=_txt(rec, 7, 21),
                    original_rdfi=_txt(rec, 28, 35),
                    trace_number=trace,
                ),
            )
        elif kind in ("05", "98", "02"):
            yield kind, None
        else:
            raise UnmappedValueError(f"NACHA line {n}: unknown addenda type {kind!r}")


def _batch(c: _Cursor) -> tuple[AchBatch, int, int, int, int]:
    head, hn = c.take("5")
    service = head[1:4]
    if service not in ("200", "220", "225", "280"):
        raise UnmappedValueError(f"NACHA line {hn}: unknown service class {service!r}")
    batch_no = _num(head, 88, 94, hn, "batch number")
    entries: list[AchEntry] = []
    count = debit = credit = hash_sum = 0
    while c.peek() == "6":
        rec, n = c.take("6")
        code = _num(rec, 2, 3, n, "transaction code")
        if code not in CREDIT_CODES and code not in DEBIT_CODES:
            raise UnmappedValueError(f"NACHA line {n}: unknown transaction code {code:02d}")
        rdfi = rec[3:11]
        if not rdfi.isdigit():
            raise _err(n, "receiving DFI id is not numeric")
        amount = _num(rec, 30, 39, n, "amount")
        indicator = rec[78]
        if indicator not in "01":
            raise _err(n, "addenda record indicator must be 0 or 1")
        trace = rec[79:94]
        addenda = list(_addenda(c, trace))
        if (indicator == "1") != bool(addenda):
            raise IntegrityError(
                f"NACHA line {n}: addenda indicator disagrees with addenda records"
            )
        returns = [r for _, r in addenda if r is not None]
        if len(returns) > 1:
            raise IntegrityError(f"NACHA line {n}: more than one return addenda")
        count += 1 + len(addenda)
        hash_sum += int(rdfi)
        if code in DEBIT_CODES:
            debit += amount
        else:
            credit += amount
        entries.append(
            AchEntry(
                transaction_code=code,
                is_debit=code in DEBIT_CODES,
                receiving_dfi=rdfi,
                account_last4=_txt(rec, 13, 29)[-4:],
                amount_minor=amount,
                trace_number=trace,
                addenda_types=tuple(k for k, _ in addenda),
                returned=returns[0] if returns else None,
            )
        )
    ctl, cn = c.take("8")
    declared = (
        ctl[1:4],
        _num(ctl, 5, 10, cn, "entry/addenda count"),
        _num(ctl, 11, 20, cn, "entry hash"),
        _num(ctl, 21, 32, cn, "total debit"),
        _num(ctl, 33, 44, cn, "total credit"),
        _num(ctl, 88, 94, cn, "batch number"),
    )
    actual = (service, count, hash_sum % 10**10, debit, credit, batch_no)
    if declared != actual:
        names = ("service class", "entry/addenda count", "entry hash", "debit", "credit", "batch")
        bad = [
            f"{k}: declared {d}, computed {a}"
            for k, d, a in zip(names, declared, actual, strict=True)
            if d != a
        ]
        raise IntegrityError(f"NACHA line {cn}: batch control mismatch ({'; '.join(bad)})")
    batch = AchBatch(
        batch_number=batch_no,
        service_class=service,
        company_name=_txt(head, 5, 20),
        company_id=_txt(head, 41, 50),
        sec_code=_txt(head, 51, 53),
        entry_description=_txt(head, 54, 63),
        effective_date=_yymmdd(head[69:75], hn),
        odfi=_txt(head, 80, 87),
        entries=tuple(entries),
    )
    return batch, count, hash_sum, debit, credit


def parse_nacha(
    data: bytes, *, allow_trimmed: bool = False, max_bytes: int = MAX_FILE_BYTES
) -> AchFile:
    """Parse and fully validate a NACHA file. Raises `IngestError`.

    `allow_trimmed=True` right-pads lines shorter than 94 characters with spaces (some
    tools strip trailing blanks); it never accepts longer lines.
    """
    check_size(data, max_bytes, "NACHA file")
    lines = _lines(data, allow_trimmed)
    if not lines:
        raise SchemaMismatchError("empty NACHA file")
    c = _Cursor(lines)
    head, hn = c.take("1")
    if head[1:3] != "01" or head[34:37] != "094" or head[37:39] != "10" or head[39] != "1":
        raise _err(hn, "file header priority, record size, blocking factor or format code is wrong")
    batches: list[AchBatch] = []
    totals = [0, 0, 0, 0]
    while c.peek() == "5":
        b, count, hsum, debit, credit = _batch(c)
        if any(x.batch_number == b.batch_number for x in batches):
            raise IntegrityError(f"NACHA: duplicate batch number {b.batch_number}")
        batches.append(b)
        for k, v in enumerate((count, hsum, debit, credit)):
            totals[k] += v
    ctl, cn = c.take("9")
    if ctl == "9" * RECORD_LEN:
        raise _err(cn, "filler record where the file control record should be")
    declared = (
        _num(ctl, 2, 7, cn, "batch count"),
        _num(ctl, 8, 13, cn, "block count"),
        _num(ctl, 14, 21, cn, "entry/addenda count"),
        _num(ctl, 22, 31, cn, "entry hash"),
        _num(ctl, 32, 43, cn, "total debit"),
        _num(ctl, 44, 55, cn, "total credit"),
    )
    for n in range(c.i, len(lines)):
        if lines[n] != "9" * RECORD_LEN:
            raise _err(n + 1, "only all-9 filler records may follow the file control")
    actual = (
        len(batches),
        math.ceil(len(lines) / 10),
        totals[0],
        totals[1] % 10**10,
        totals[2],
        totals[3],
    )
    if declared != actual:
        names = ("batches", "blocks", "entry/addenda count", "entry hash", "debit", "credit")
        bad = [
            f"{k}: declared {d}, computed {a}"
            for k, d, a in zip(names, declared, actual, strict=True)
            if d != a
        ]
        raise IntegrityError(f"NACHA line {cn}: file control mismatch ({'; '.join(bad)})")
    return AchFile(
        immediate_destination=_txt(head, 4, 13),
        immediate_origin=_txt(head, 14, 23),
        creation_date=_yymmdd(head[23:29], hn),
        batches=tuple(batches),
    )
