"""BAI2 cash management balance reporting parser.

Record layout and integrity rules follow ministryofjustice/bai2 (`bai2/parsers.py`,
`bai2/helpers.py` at 6696a72, MIT) and were checked against the real sample files in
its `tests/data/` (vendored in tests/fixtures/bank/):

- records 01 file header, 02 group header, 03 account identifier, 16 transaction
  detail, 88 continuation, 49 account trailer, 98 group trailer, 99 file trailer;
  fields are comma separated and a record ends with "/" (the free-text tail of a 16
  record does not); 88 continues the previous record.
- 49 control total = sum of the 03 summary amounts + the 16 amounts; 98 = sum of its
  49 totals; 99 = sum of its 98 totals; record counts are physical lines (88s included)
  from header to trailer; 98 and 99 also count accounts and groups.
- funds type S carries 3 availability amounts, V a date and time, D a count and that
  many (day, amount) pairs.
- dates are YYMMDD (Python's %y pivot); version must be 2.

ASSUMPTION: amounts are read as minor units of the account currency (BAI2 amounts
carry implied decimals). A missing currency at both group and account level is an
error unless the caller passes `default_currency` (the library defaults to USD; we do
not guess).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime

from pydantic import ValidationError

from scruple.ingest.bank.bai2_codes import CREDIT_CODES, DEBIT_CODES, MISC_CODES, STATUS_CODES
from scruple.ingest.bank.models import (
    BankStatement,
    Direction,
    StatementBalance,
    StatementEntry,
    truncate,
)
from scruple.ingest.currency import normalise_currency
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    IntegrityError,
    MalformedInputError,
    SchemaMismatchError,
    UnmappedValueError,
)
from scruple.ingest.limits import MAX_FILE_BYTES, check_size, decode_utf8

_CODES = {"01", "02", "03", "16", "49", "88", "98", "99"}
_INT = re.compile(r"^[+-]?[0-9]{1,18}$")
MAX_LINE = 4096


@dataclass(frozen=True)
class _Record:
    code: str
    fields: list[str]
    rows: int
    line: int


def _err(line: int, msg: str) -> MalformedInputError:
    return MalformedInputError(f"BAI2 line {line}: {msg}")


def _int(value: str, line: int, what: str) -> int | None:
    v = value.strip()
    if not v:
        return None
    if not _INT.match(v):
        raise _err(line, f"{what} {v[:20]!r} is not an integer")
    return int(v)


def _date(value: str, line: int) -> date | None:
    v = value.strip()
    if not v:
        return None
    if not re.fullmatch(r"[0-9]{6}", v):
        raise _err(line, f"date {v[:12]!r} is not YYMMDD")
    try:
        return datetime.strptime(v, "%y%m%d").date()
    except ValueError:
        raise _err(line, f"date {v!r} is not a calendar date") from None


def _join(code: str, rows: list[str]) -> list[str]:
    """Join a record's physical rows into fields (ported from bai2/helpers.py)."""
    text = ""
    for i, row in enumerate(rows):
        if code == "03":
            commas = row.count(",") - (2 if i == 0 else 0)
            if row.endswith("/"):
                text += row[:-1]
                if commas % 4 != 0:
                    text += ","
            else:
                text += row
        elif row:
            text += row[:-1] + "," if row.endswith("/") else row + " "
    if code != "03":
        text = text[:-1]
    return text.split(",")


def _records(text: str) -> Iterator[_Record]:
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    pending: tuple[str, list[str], int] | None = None
    for n, raw in enumerate(lines, start=1):
        if len(raw) > MAX_LINE:
            raise InputTooLargeError(f"BAI2 line {n} is longer than {MAX_LINE} characters")
        line = raw.rstrip()
        if len(line) < 3 or line[:2] not in _CODES or line[2] != ",":
            raise _err(n, "does not start with a record code and a comma")
        code, rest = line[:2], line[3:]
        if code == "88":
            if pending is None:
                raise _err(n, "continuation record with nothing to continue")
            pending[1].append(rest)
            continue
        if pending is not None:
            yield _Record(pending[0], _join(pending[0], pending[1]), len(pending[1]), pending[2])
        pending = (code, [rest], n)
    if pending is not None:
        yield _Record(pending[0], _join(pending[0], pending[1]), len(pending[1]), pending[2])


class _Stream:
    def __init__(self, records: Iterator[_Record]) -> None:
        self._it = records
        self.cur: _Record | None = next(self._it, None)

    def peek(self) -> str | None:
        return self.cur.code if self.cur else None

    def take(self, code: str) -> _Record:
        rec = self.cur
        if rec is None:
            raise MalformedInputError(f"BAI2 ended early: expected record {code}")
        if rec.code != code:
            raise _err(rec.line, f"expected record {code}, got {rec.code}")
        self.cur = next(self._it, None)
        return rec


def _availability(funds: str, rest: list[str], line: int) -> list[str]:
    take = {"S": 3, "V": 2}.get(funds, 0)
    if funds == "D":
        if not rest:
            raise _err(line, "funds type D without a distribution count")
        count = _int(rest[0], line, "distribution count") or 0
        if not 0 <= count <= 64:
            raise _err(line, "distribution count out of range")
        take = 1 + 2 * count
    elif funds not in ("", "0", "1", "2", "S", "V", "Z"):
        raise _err(line, f"unknown funds type {funds[:4]!r}")
    if len(rest) < take:
        raise _err(line, "availability fields are missing")
    return rest[take:]


def _direction(code: str, line: int) -> Direction | None:
    if code in CREDIT_CODES:
        return Direction.CREDIT
    if code in DEBIT_CODES:
        return Direction.DEBIT
    if code in MISC_CODES:
        return None
    if code in STATUS_CODES:
        raise _err(line, f"status code {code} used as a transaction")
    raise UnmappedValueError(f"BAI2 line {line}: unknown type code {code[:4]!r}")


def _account(
    s: _Stream, group_ccy: str | None, default_ccy: str | None, as_of: date | None, check: bool
) -> tuple[BankStatement, int, int]:
    head = s.take("03")
    f = head.fields
    if len(f) < 2 or not f[0].strip():
        raise _err(head.line, "account identifier needs an account number")
    raw_ccy = f[1].strip() or group_ccy or default_ccy
    if not raw_ccy:
        raise _err(head.line, "no currency on account or group; pass default_currency")
    ccy = normalise_currency(raw_ccy)
    rest = f[2:]
    total = 0
    balances: list[StatementBalance] = []
    while rest:
        if len(rest) == 1 and not rest[0]:
            break
        if len(rest) < 4:
            raise _err(head.line, "incomplete summary item")
        code, amount_s, _count, funds = (x.strip() for x in rest[:4])
        amount = _int(amount_s, head.line, "summary amount")
        rest = _availability(funds, rest[4:], head.line)
        total += amount or 0
        if code in STATUS_CODES and amount is not None:
            balances.append(StatementBalance(kind=code, amount_minor=amount, as_of=as_of))
    rows = head.rows
    entries: list[StatementEntry] = []
    while s.peek() == "16":
        rec = s.take("16")
        rows += rec.rows
        g = rec.fields
        if len(g) < 3:
            raise _err(rec.line, "transaction detail is too short")
        code = g[0].strip()
        amount = _int(g[1], rec.line, "amount")
        if amount is None or amount < 0:
            raise _err(rec.line, "transaction amount must be a non-negative integer")
        tail = _availability(g[2].strip(), g[3:], rec.line)
        total += amount
        entries.append(
            StatementEntry(
                amount_minor=amount,
                direction=_direction(code, rec.line),
                type_code=code,
                bank_ref=truncate(tail[0], 64) if len(tail) > 0 else None,
                customer_ref=truncate(tail[1], 64) if len(tail) > 1 else None,
                description=truncate(",".join(tail[2:]), 500) if len(tail) > 2 else None,
                booking_date=as_of,
            )
        )
    trailer = s.take("49")
    rows += trailer.rows
    t = trailer.fields
    control = (_int(t[0], trailer.line, "account control total") or 0) if t else 0
    count = _int(t[1], trailer.line, "record count") if len(t) > 1 else None
    if check:
        if control != total:
            raise IntegrityError(
                f"BAI2 line {trailer.line}: account control total {control}, records sum to {total}"
            )
        if count != rows:
            raise IntegrityError(
                f"BAI2 line {trailer.line}: {count} records declared, {rows} found"
            )
    stmt = BankStatement(
        format="bai2",
        account_id=f[0].strip()[:64],
        currency=ccy,
        balances=tuple(balances),
        entries=tuple(entries),
    )
    return stmt, control, rows


def parse_bai2(
    data: bytes | str,
    *,
    default_currency: str | None = None,
    max_bytes: int = MAX_FILE_BYTES,
    check_integrity: bool = True,
) -> tuple[BankStatement, ...]:
    """Parse a BAI2 file into one statement per account. Raises `IngestError`."""
    check_size(data, max_bytes, "BAI2 file")
    text = decode_utf8(data, "BAI2 file") if isinstance(data, bytes) else data
    if "\x00" in text:
        raise MalformedInputError("BAI2 file contains NUL bytes")
    try:
        return _parse(text, default_currency, check_integrity)
    except ValidationError as e:
        raise SchemaMismatchError(f"BAI2 value out of range: {e.error_count()} errors") from None
    except IngestError:
        raise


def _parse(text: str, default_ccy: str | None, check: bool) -> tuple[BankStatement, ...]:
    s = _Stream(_records(text))
    head = s.take("01")
    if len(head.fields) < 8 or head.fields[7].strip() != "2":
        raise _err(head.line, "only BAI version 2 is supported")
    file_rows = head.rows
    file_total = 0
    groups = 0
    out: list[BankStatement] = []
    while s.peek() == "02":
        g = s.take("02")
        gf = g.fields
        group_rows = g.rows
        as_of = _date(gf[3], g.line) if len(gf) > 3 else None
        group_ccy = gf[5].strip() if len(gf) > 5 and gf[5].strip() else None
        group_total = 0
        accounts = 0
        while s.peek() == "03":
            stmt, control, rows = _account(s, group_ccy, default_ccy, as_of, check)
            out.append(stmt)
            group_total += control
            group_rows += rows
            accounts += 1
        if accounts == 0:
            raise _err(g.line, "group without accounts")
        gt = s.take("98")
        group_rows += gt.rows
        tf = gt.fields
        declared = [_int(x, gt.line, "group trailer") for x in [*tf, "", "", ""][:3]]
        if check and declared != [group_total, accounts, group_rows]:
            raise IntegrityError(
                f"BAI2 line {gt.line}: group trailer {declared} != "
                f"[{group_total}, {accounts}, {group_rows}]"
            )
        file_total += declared[0] or 0
        file_rows += group_rows
        groups += 1
    if groups == 0:
        raise _err(head.line, "file without groups")
    ft = s.take("99")
    file_rows += ft.rows
    ff = ft.fields
    declared = [_int(x, ft.line, "file trailer") for x in [*ff, "", "", ""][:3]]
    if check and declared != [file_total, groups, file_rows]:
        raise IntegrityError(
            f"BAI2 line {ft.line}: file trailer {declared} != [{file_total}, {groups}, {file_rows}]"
        )
    if s.cur is not None:
        raise _err(s.cur.line, "records after the file trailer")
    return tuple(out)
