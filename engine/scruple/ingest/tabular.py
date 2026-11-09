"""Generic case import from CSV and JSON Lines with a declared column mapping.

The mapping is a Pydantic config (`ImportConfig`): which column holds which field, how
amounts are written, and how the source's status and reason values map onto scruple's.
Validation is strict and row-level: every data row ends up either as a case or as one
or more `RowError`s in the report; nothing is dropped silently
(`rows_read == len(cases) + rows_with_errors`, tested as a property).

Parquet is deferred: pyarrow is a large native dependency for one format. Export to
CSV or JSONL (for example `pyarrow.csv.write_csv`) and import that.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scruple.domain import ReasonCode
from scruple.ingest.currency import decimal_to_minor, normalise_currency
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    MalformedInputError,
    SchemaMismatchError,
)
from scruple.ingest.limits import MAX_FILE_BYTES, check_size, decode_utf8, load_json_object
from scruple.ingest.models import CaseState, ExternalCase, SourceName
from scruple.sim.models import Stage


class StatusTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Stage
    state: CaseState


class ColumnMapping(BaseModel):
    """Source column name for each `ExternalCase` field. Optional fields may be None."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str = Field(min_length=1, max_length=128)
    payment_ref: str = Field(min_length=1, max_length=128)
    amount: str = Field(min_length=1, max_length=128)
    currency: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=128)
    opened_at: str = Field(min_length=1, max_length=128)
    merchant_ref: str | None = Field(default=None, min_length=1, max_length=128)
    network: str | None = Field(default=None, min_length=1, max_length=128)
    network_reason_code: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_due_at: str | None = Field(default=None, min_length=1, max_length=128)

    def used(self) -> dict[str, str]:
        """field -> column, for the columns that are mapped."""
        return {k: v for k, v in self.model_dump().items() if v is not None}

    @model_validator(mode="after")
    def _distinct(self) -> ColumnMapping:
        cols = list(self.used().values())
        if len(cols) != len(set(cols)):
            raise ValueError("two fields are mapped to the same column")
        return self


class ImportConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: SourceName = "csv"
    columns: ColumnMapping
    amount_format: Literal["minor", "decimal"] = "decimal"
    status_map: dict[str, StatusTarget] = Field(min_length=1)
    reason_map: dict[str, ReasonCode | None] = Field(min_length=1)
    allow_extra_columns: bool = False
    delimiter: Literal[",", ";", "\t", "|"] = ","
    max_rows: int = Field(default=100_000, ge=1, le=5_000_000)
    max_bytes: int = Field(default=MAX_FILE_BYTES, ge=1, le=1024 * 1024 * 1024)
    max_field_chars: int = Field(default=4096, ge=16, le=1_000_000)
    max_errors: int = Field(default=10_000, ge=1)


class RowError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: int  # 1-based data row (the header is row 0)
    column: str | None
    message: str


class ImportFailedError(IngestError):
    def __init__(self, report: ImportReport) -> None:
        n = len({e.row for e in report.errors})
        super().__init__(f"{n} of {report.rows_read} rows failed validation")
        self.report = report


class ImportReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_read: int
    cases: tuple[ExternalCase, ...]
    errors: tuple[RowError, ...]
    truncated_errors: bool = False

    @property
    def rows_failed(self) -> int:
        return self.rows_read - len(self.cases)

    def raise_if_errors(self) -> ImportReport:
        if self.errors:
            raise ImportFailedError(self)
        return self


def _parse_when(text: str) -> datetime:
    s = text.strip()
    if len(s) == 10:
        return datetime.combine(date.fromisoformat(s), datetime.min.time(), UTC)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("timestamp needs a UTC offset (or use a plain date)")
    return dt


class _RowMapper:
    def __init__(self, cfg: ImportConfig) -> None:
        self.cfg = cfg
        self.cols = cfg.columns.used()
        self.seen: dict[str, int] = {}

    def check_header(self, header: list[str]) -> None:
        if len(header) != len(set(header)):
            raise SchemaMismatchError("duplicate column names in header")
        missing = [c for c in self.cols.values() if c not in header]
        if missing:
            raise SchemaMismatchError(f"missing mapped columns: {sorted(missing)}")
        extra = set(header) - set(self.cols.values())
        if extra and not self.cfg.allow_extra_columns:
            shown = sorted(e[:32] for e in extra)[:10]
            raise SchemaMismatchError(f"unmapped columns (set allow_extra_columns): {shown}")

    def map(self, n: int, rec: dict[str, str]) -> ExternalCase | list[RowError]:
        cfg = self.cfg
        errs: list[RowError] = []

        def err(field: str | None, msg: str) -> None:
            errs.append(RowError(row=n, column=self.cols.get(field or "", field), message=msg))

        for col, val in rec.items():
            if len(val) > cfg.max_field_chars:
                err(None, f"column {col[:32]!r} longer than {cfg.max_field_chars} chars")
                return errs

        def get(field: str) -> str:
            col = self.cols.get(field)
            return rec.get(col, "").strip() if col else ""

        values: dict[str, Any] = {"source": cfg.source}
        for field in ("external_id", "payment_ref"):
            v = get(field)
            if not v:
                err(field, "required value is empty")
            values[field] = v
        for field in ("merchant_ref", "network", "network_reason_code"):
            values[field] = get(field) or None

        currency: str | None = None
        try:
            currency = normalise_currency(get("currency"))
            values["currency"] = currency
        except IngestError as e:
            err("currency", str(e))

        raw_amount = get("amount")
        if cfg.amount_format == "minor":
            if raw_amount.isascii() and raw_amount.isdigit() and len(raw_amount) <= 18:
                values["amount_minor"] = int(raw_amount)
            else:
                err("amount", f"{raw_amount[:32]!r} is not a non-negative integer of minor units")
        elif currency is not None:
            try:
                amt = decimal_to_minor(raw_amount, currency)
                if amt < 0:
                    err("amount", "amount is negative")
                values["amount_minor"] = amt
            except IngestError as e:
                err("amount", str(e))

        status = get("status")
        target = cfg.status_map.get(status)
        if target is None:
            err("status", f"status {status[:32]!r} is not in status_map")
        else:
            values["stage"] = target.stage
            values["state"] = target.state
        reason = get("reason")
        if reason not in cfg.reason_map:
            err("reason", f"reason {reason[:32]!r} is not in reason_map")
        else:
            values["reason_code"] = cfg.reason_map[reason]
            values["source_reason"] = reason

        for field, required in (("opened_at", True), ("evidence_due_at", False)):
            v = get(field)
            if not v:
                if required:
                    err(field, "required value is empty")
                continue
            try:
                values[field] = _parse_when(v)
            except ValueError as e:
                err(field, f"{v[:40]!r}: {e}")

        ext = values["external_id"]
        if ext and ext in self.seen:
            err("external_id", f"duplicate of row {self.seen[ext]}")
        if errs:
            return errs
        try:
            case = ExternalCase(**values)
        except ValidationError as e:
            return [
                RowError(row=n, column=self.cols.get(str(x["loc"][0])), message=x["msg"][:200])
                for x in e.errors()
            ]
        self.seen[ext] = n
        return case


def _collect(
    cfg: ImportConfig, header: list[str], rows: Iterable[tuple[int, dict[str, str] | RowError]]
) -> ImportReport:
    mapper = _RowMapper(cfg)
    mapper.check_header(header)
    cases: list[ExternalCase] = []
    errors: list[RowError] = []
    truncated = False
    n_rows = 0
    for n, rec in rows:
        n_rows = n
        if n > cfg.max_rows:
            raise InputTooLargeError(f"more than {cfg.max_rows} rows")
        out = rec if isinstance(rec, RowError) else mapper.map(n, rec)
        if isinstance(out, ExternalCase):
            cases.append(out)
            continue
        batch = [out] if isinstance(out, RowError) else out
        room = cfg.max_errors - len(errors)
        if len(batch) > room:
            truncated = True  # the row still counts as failed via rows_read - len(cases)
        errors.extend(batch[: max(room, 0)])
    return ImportReport(
        rows_read=n_rows, cases=tuple(cases), errors=tuple(errors), truncated_errors=truncated
    )


def _text(data: bytes | str, cfg: ImportConfig) -> str:
    check_size(data, cfg.max_bytes, "import file")
    text = decode_utf8(data, "import file") if isinstance(data, bytes) else data
    if "\x00" in text:
        raise MalformedInputError("import file contains NUL bytes")
    return text.removeprefix("﻿")


Rows = Iterable[tuple[int, dict[str, str] | RowError]]


def import_rows(cfg: ImportConfig, header: list[str], rows: Rows) -> ImportReport:
    """Map already split rows (row number, column -> value, or a `RowError`)."""
    return _collect(cfg, header, rows)


def import_csv(data: bytes | str, cfg: ImportConfig, *, strict: bool = False) -> ImportReport:
    """Import cases from CSV. With `strict=True`, any row error raises `ImportFailedError`."""
    header, rows = csv_rows(data, cfg)
    report = _collect(cfg, header, rows)
    return report.raise_if_errors() if strict else report


def csv_rows(
    data: bytes | str, cfg: ImportConfig
) -> tuple[list[str], Iterator[tuple[int, dict[str, str] | RowError]]]:
    """The header and the data rows of a CSV file, before any mapping."""
    text = _text(data, cfg)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=cfg.delimiter, strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise SchemaMismatchError("empty file: no header row") from None
    except csv.Error as e:
        raise MalformedInputError(f"bad CSV header: {e}") from None
    header = [h.strip() for h in header]

    def rows() -> Iterator[tuple[int, dict[str, str] | RowError]]:
        n = 0
        while True:
            n += 1
            try:
                fields = next(reader)
            except StopIteration:
                return
            except csv.Error as e:
                # The csv module cannot resynchronise after a quoting error.
                yield (
                    n,
                    RowError(
                        row=n, column=None, message=f"bad CSV, reading stopped here: {str(e)[:120]}"
                    ),
                )
                return
            if len(fields) != len(header):
                yield (
                    n,
                    RowError(
                        row=n,
                        column=None,
                        message=f"has {len(fields)} fields, header has {len(header)}",
                    ),
                )
                continue
            yield n, dict(zip(header, fields, strict=True))

    return header, rows()


def import_jsonl(data: bytes | str, cfg: ImportConfig, *, strict: bool = False) -> ImportReport:
    """Import cases from JSON Lines: one object per line, keys are the column names.

    Values must be strings (an integer is accepted for a minor-unit amount); null
    means empty. The header is the union of keys on the first line.
    """
    header, parsed = jsonl_rows(data, cfg)
    report = _collect(cfg, header, parsed)
    return report.raise_if_errors() if strict else report


def jsonl_rows(
    data: bytes | str, cfg: ImportConfig
) -> tuple[list[str], list[tuple[int, dict[str, str] | RowError]]]:
    """The header (keys of the first object) and the rows of a JSON Lines file."""
    text = _text(data, cfg)
    lines = text.splitlines()
    if not lines:
        raise SchemaMismatchError("empty file")
    first: dict[str, Any] | None = None
    parsed: list[tuple[int, dict[str, str] | RowError]] = []
    for n, line in enumerate(lines, start=1):
        if n > cfg.max_rows:
            raise InputTooLargeError(f"more than {cfg.max_rows} rows")
        try:
            obj = load_json_object(line, max_bytes=cfg.max_field_chars * 64, max_depth=2)
        except IngestError as e:
            parsed.append((n, RowError(row=n, column=None, message=str(e)[:200])))
            continue
        if first is None:
            first = obj
        rec: dict[str, str] = {}
        bad: RowError | None = None
        for k, v in obj.items():
            if v is None:
                rec[k] = ""
            elif isinstance(v, str):
                rec[k] = v
            elif (
                isinstance(v, int)
                and not isinstance(v, bool)
                and k == cfg.columns.amount
                and cfg.amount_format == "minor"
            ):
                rec[k] = str(v)
            else:
                bad = RowError(row=n, column=k[:64], message="value must be a string or null")
                break
        if bad is None and first is not None and set(obj) != set(first):
            bad = RowError(row=n, column=None, message="keys differ from the first line")
        parsed.append((n, bad if bad is not None else rec))
    if first is None:
        raise SchemaMismatchError("no line is a JSON object")
    return list(first), parsed
