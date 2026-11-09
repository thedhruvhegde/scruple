"""Bank statement CSV import with a declared column mapping.

Banks export CSV in many shapes; the caller declares which column is which and
whether amounts are one signed column or separate debit and credit columns. Same
rules as `scruple.ingest.tabular`: row-level errors, nothing dropped silently.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scruple.ingest.bank.models import BankStatement, Direction, StatementEntry, truncate
from scruple.ingest.currency import decimal_to_minor, normalise_currency
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    MalformedInputError,
    SchemaMismatchError,
)
from scruple.ingest.limits import MAX_FILE_BYTES, check_size, decode_utf8
from scruple.ingest.tabular import RowError


class BankCsvConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)
    booking_date: str
    amount: str | None = None  # one signed column (negative = debit) ...
    debit: str | None = None  # ... or separate unsigned debit and credit columns
    credit: str | None = None
    value_date: str | None = None
    reference: str | None = None
    description: str | None = None
    date_format: Literal["iso", "dmy", "mdy"] = "iso"
    delimiter: Literal[",", ";", "\t", "|"] = ","
    max_rows: int = Field(default=1_000_000, ge=1)
    max_bytes: int = Field(default=MAX_FILE_BYTES, ge=1)

    @model_validator(mode="after")
    def _amount_columns(self) -> BankCsvConfig:
        signed = self.amount is not None
        split = self.debit is not None and self.credit is not None
        if signed == split or (not split and (self.debit or self.credit)):
            raise ValueError("declare either amount, or both debit and credit")
        return self


class BankCsvReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_read: int
    statement: BankStatement
    errors: tuple[RowError, ...]


def _date(text: str, fmt: str) -> date:
    s = text.strip()
    if fmt == "iso":
        return date.fromisoformat(s)
    parts = s.replace(".", "/").replace("-", "/").split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts) or len(parts[2]) != 4:
        raise ValueError(f"{s[:16]!r} is not a {fmt} date with a 4-digit year")
    a, b, y = (int(p) for p in parts)
    return date(y, b, a) if fmt == "dmy" else date(y, a, b)


def import_bank_csv(data: bytes | str, cfg: BankCsvConfig) -> BankCsvReport:
    check_size(data, cfg.max_bytes, "bank CSV")
    text = decode_utf8(data, "bank CSV") if isinstance(data, bytes) else data
    if "\x00" in text:
        raise MalformedInputError("bank CSV contains NUL bytes")
    ccy = normalise_currency(cfg.currency)
    reader = csv.reader(
        io.StringIO(text.removeprefix("﻿"), newline=""), delimiter=cfg.delimiter, strict=True
    )
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        raise SchemaMismatchError("empty bank CSV") from None
    except csv.Error as e:
        raise MalformedInputError(f"bad CSV header: {e}") from None
    wanted = [
        c
        for c in (
            cfg.booking_date,
            cfg.amount,
            cfg.debit,
            cfg.credit,
            cfg.value_date,
            cfg.reference,
            cfg.description,
        )
        if c is not None
    ]
    missing = [c for c in wanted if c not in header]
    if missing or len(set(header)) != len(header):
        raise SchemaMismatchError(f"missing or duplicate columns: {sorted(missing)}")
    entries: list[StatementEntry] = []
    errors: list[RowError] = []
    n = 0
    while True:
        try:
            fields = next(reader)
        except StopIteration:
            break
        except csv.Error as e:
            n += 1
            errors.append(
                RowError(row=n, column=None, message=f"bad CSV, reading stopped here: {e}")
            )
            break
        n += 1
        if n > cfg.max_rows:
            raise InputTooLargeError(f"more than {cfg.max_rows} rows")
        if len(fields) != len(header):
            errors.append(RowError(row=n, column=None, message="wrong number of fields"))
            continue
        rec = dict(zip(header, fields, strict=True))
        try:
            if cfg.amount is not None:
                signed = decimal_to_minor(rec[cfg.amount], ccy)
            else:
                assert cfg.debit is not None
                assert cfg.credit is not None
                d_raw, c_raw = rec[cfg.debit].strip(), rec[cfg.credit].strip()
                if bool(d_raw) == bool(c_raw):
                    raise SchemaMismatchError("exactly one of debit and credit must be filled")
                signed = -decimal_to_minor(d_raw, ccy) if d_raw else decimal_to_minor(c_raw, ccy)
                if (d_raw and signed > 0) or (c_raw and signed < 0):
                    raise SchemaMismatchError("debit and credit columns must be unsigned")
            if signed == 0:
                raise SchemaMismatchError("zero amount")
            entries.append(
                StatementEntry(
                    amount_minor=abs(signed),
                    direction=Direction.CREDIT if signed > 0 else Direction.DEBIT,
                    booking_date=_date(rec[cfg.booking_date], cfg.date_format),
                    value_date=_date(rec[cfg.value_date], cfg.date_format)
                    if cfg.value_date and rec[cfg.value_date].strip()
                    else None,
                    customer_ref=truncate(rec[cfg.reference], 64) if cfg.reference else None,
                    description=truncate(rec[cfg.description], 500) if cfg.description else None,
                )
            )
        except (IngestError, ValueError, ValidationError) as e:
            errors.append(RowError(row=n, column=None, message=str(e)[:200]))
    stmt = BankStatement(
        format="csv", account_id=cfg.account_id, currency=ccy, entries=tuple(entries)
    )
    return BankCsvReport(rows_read=n, statement=stmt, errors=tuple(errors))
