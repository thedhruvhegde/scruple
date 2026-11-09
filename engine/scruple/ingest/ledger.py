"""Ledger port, an in-memory and a generic double-entry JSON adapter, and payload
mappings for Formance Ledger and Modern Treasury.

Internal convention (the same as `scruple.sim.ledger`): a transaction is a set of
signed lines in one currency that sum to zero; a negative line is money leaving an
account, a positive line money arriving.

Generic JSON document (one transaction per line in `JsonFileLedger`):
    {"external_id": "...", "occurred_at": "<ISO 8601 with offset>", "currency": "USD",
     "description": null, "metadata": {},
     "entries": [{"account": "merchant:m1", "direction": "credit", "amount": 1000},
                 {"account": "cardholder:t1", "direction": "debit", "amount": 1000}]}
`debit` is a positive line, `credit` a negative one, amounts are positive minor units.

Formance (formancehq/ledger `openapi.yml` at c0fe3f5, Ledger v3 API, fetched 2026-11-09):
`CreateTransactionRequest` has `postings` (`PostingRequest`: required `source`,
`destination`, `amount` unsigned integer, `asset` such as "USD/2"), `reference`,
`timestamp` (ISO 8601) and `metadata`. Account addresses are non-empty, contain only
letters, digits, colons, underscores and hyphens, with no empty segments. No licence
file was found in that repository, so nothing from it is vendored; only these facts
are used.

Modern Treasury (Modern-Treasury/modern-treasury-python at cf80342, MIT, generated
from their OpenAPI spec): `LedgerTransactionCreateParams` has required
`ledger_entries` (`LedgerEntryCreateRequest`: required `direction` in
{"credit", "debit"} and `ledger_account_id`; `amount` in the currency's smallest
unit), plus `external_id`, `effective_at`, `description`, `metadata` (str -> str) and
`status` in {"archived", "pending", "posted"}.
This is a mapping stub: no Modern Treasury or Formance HTTP client is written.
ASSUMPTION: MT entry direction depends on each ledger account's normal balance; the
mapper treats every account as debit-normal (positive line -> debit). Review before use.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from scruple.ingest.currency import Currency, minor_units
from scruple.ingest.errors import (
    IngestError,
    MalformedInputError,
    SchemaMismatchError,
    UnmappedValueError,
)
from scruple.ingest.limits import MAX_FILE_BYTES, check_size, decode_utf8, load_json_object
from scruple.ingest.models import LedgerMovement
from scruple.sim.ledger import JournalEntry

_SEGMENT = r"[A-Za-z0-9_-]+"
ACCOUNT_PATTERN = rf"^{_SEGMENT}(:{_SEGMENT})*$"
Account = Annotated[str, StringConstraints(pattern=ACCOUNT_PATTERN, max_length=255)]
MetaKey = Annotated[str, StringConstraints(min_length=1, max_length=128)]
MetaValue = Annotated[str, StringConstraints(max_length=1024)]


class LedgerLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account: Account
    amount_minor: int = Field(ge=-(10**30), le=10**30)

    @model_validator(mode="after")
    def _nonzero(self) -> LedgerLine:
        if self.amount_minor == 0:
            raise ValueError("a ledger line cannot be zero")
        return self


class LedgerTransaction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str = Field(min_length=1, max_length=255)
    occurred_at: AwareDatetime
    currency: Currency
    lines: tuple[LedgerLine, ...] = Field(min_length=2, max_length=128)
    description: str | None = Field(default=None, max_length=500)
    metadata: dict[MetaKey, MetaValue] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def _balanced(self) -> LedgerTransaction:
        if sum(line.amount_minor for line in self.lines) != 0:
            raise ValueError("transaction lines must sum to zero")
        return self

    def net_by_account(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for line in self.lines:
            out[line.account] += line.amount_minor
        return {k: v for k, v in sorted(out.items()) if v}


class LedgerConflictError(IngestError):
    """A transaction id was posted before with different content."""


class LedgerPort(Protocol):
    """What scruple needs from a ledger. Posting is idempotent by `external_id`."""

    def post(self, txn: LedgerTransaction) -> bool:
        """True if newly posted, False if an identical transaction already exists."""
        ...

    def get(self, external_id: str) -> LedgerTransaction | None: ...

    def transactions(self) -> tuple[LedgerTransaction, ...]: ...

    def balance(self, account: str, currency: str) -> int: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self._txns: dict[str, LedgerTransaction] = {}
        self._bal: dict[tuple[str, str], int] = defaultdict(int)

    def post(self, txn: LedgerTransaction) -> bool:
        prior = self._txns.get(txn.external_id)
        if prior is not None:
            if prior != txn:
                raise LedgerConflictError(f"{txn.external_id} already posted with other content")
            return False
        self._txns[txn.external_id] = txn
        for line in txn.lines:
            self._bal[(line.account, txn.currency)] += line.amount_minor
        return True

    def get(self, external_id: str) -> LedgerTransaction | None:
        return self._txns.get(external_id)

    def transactions(self) -> tuple[LedgerTransaction, ...]:
        return tuple(self._txns.values())

    def balance(self, account: str, currency: str) -> int:
        return self._bal.get((account, currency), 0)


# --- generic double-entry JSON ----------------------------------------------------


class _Entry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    account: Account
    direction: Literal["debit", "credit"]
    amount: int = Field(gt=0, le=10**30)


class _Doc(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str
    occurred_at: str
    currency: str
    description: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    entries: list[_Entry]


def to_json_doc(txn: LedgerTransaction) -> dict[str, Any]:
    return {
        "external_id": txn.external_id,
        "occurred_at": txn.occurred_at.isoformat(),
        "currency": txn.currency,
        "description": txn.description,
        "metadata": dict(txn.metadata),
        "entries": [
            {
                "account": line.account,
                "direction": "debit" if line.amount_minor > 0 else "credit",
                "amount": abs(line.amount_minor),
            }
            for line in txn.lines
        ],
    }


def from_json_doc(doc: Mapping[str, Any]) -> LedgerTransaction:
    try:
        d = _Doc.model_validate(doc)
        return LedgerTransaction.model_validate(
            {
                "external_id": d.external_id,
                "occurred_at": d.occurred_at,
                "currency": d.currency,
                "description": d.description,
                "metadata": d.metadata,
                "lines": [
                    {
                        "account": e.account,
                        "amount_minor": e.amount if e.direction == "debit" else -e.amount,
                    }
                    for e in d.entries
                ],
            }
        )
    except ValidationError as e:
        raise SchemaMismatchError(f"not a ledger transaction: {e.error_count()} errors") from None


class JsonFileLedger(InMemoryLedger):
    """Append-only JSON Lines file of generic documents; reloads and re-validates on open."""

    def __init__(self, path: str | Path, *, max_bytes: int = MAX_FILE_BYTES) -> None:
        super().__init__()
        self._path = Path(path)
        if self._path.exists():
            data = self._path.read_bytes()
            check_size(data, max_bytes, "ledger file")
            for n, line in enumerate(decode_utf8(data, "ledger file").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    super().post(from_json_doc(load_json_object(line)))
                except IngestError as e:
                    raise MalformedInputError(f"ledger file line {n}: {e}") from None

    def post(self, txn: LedgerTransaction) -> bool:
        if not super().post(txn):
            return False
        line = json.dumps(to_json_doc(txn), sort_keys=True, separators=(",", ":"))
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True


# --- conversions from scruple records -------------------------------------------


def from_movement(m: LedgerMovement, merchant_account: str) -> tuple[LedgerTransaction, ...]:
    out = []
    for kind, _txn, ccy, postings in m.journal_postings(merchant_account):
        out.append(
            LedgerTransaction(
                external_id=f"{m.source}:{m.external_id}:{kind}",
                occurred_at=m.occurred_at,
                currency=ccy,
                lines=tuple(LedgerLine(account=a, amount_minor=v) for a, v in postings),
                metadata={"case": m.case_external_id, "payment": m.payment_ref, "kind": kind},
            )
        )
    return tuple(out)


def from_journal(entry: JournalEntry, occurred_at: Any, prefix: str = "sim") -> LedgerTransaction:
    """A simulator journal entry as a ledger transaction (for exporting a sim ledger)."""
    try:
        return LedgerTransaction(
            external_id=f"{prefix}:{entry.entry_id}",
            occurred_at=occurred_at,
            currency=entry.currency,
            lines=tuple(
                LedgerLine(account=p.account, amount_minor=p.amount_minor)
                for p in entry.postings
                if p.amount_minor
            ),
            metadata={"kind": entry.kind, "txn": entry.txn_id}
            | ({"case": entry.case_id} if entry.case_id else {}),
        )
    except ValidationError as e:
        raise SchemaMismatchError(
            f"journal entry {entry.entry_id}: {e.error_count()} errors"
        ) from None


# --- Formance -------------------------------------------------------------------


def formance_asset(currency: str) -> str:
    return f"{currency}/{minor_units(currency)}"


def _pairs(txn: LedgerTransaction) -> list[tuple[str, str, int]]:
    """Decompose signed lines into (source, destination, amount) postings, greedily."""
    net = txn.net_by_account()
    src_acc = [a for a, v in net.items() if v < 0]
    src_amt = [-v for v in net.values() if v < 0]
    dst_acc = [a for a, v in net.items() if v > 0]
    dst_amt = [v for v in net.values() if v > 0]
    out: list[tuple[str, str, int]] = []
    i = j = 0
    while i < len(src_acc) and j < len(dst_acc):
        amt = min(src_amt[i], dst_amt[j])
        out.append((src_acc[i], dst_acc[j], amt))
        src_amt[i] -= amt
        dst_amt[j] -= amt
        if src_amt[i] == 0:
            i += 1
        if dst_amt[j] == 0:
            j += 1
    return out


def to_formance(txn: LedgerTransaction) -> dict[str, Any]:
    """A Formance v3 `CreateTransactionRequest` body."""
    asset = formance_asset(txn.currency)
    return {
        "postings": [
            {"source": s, "destination": d, "amount": a, "asset": asset} for s, d, a in _pairs(txn)
        ],
        "reference": txn.external_id,
        "timestamp": txn.occurred_at.isoformat(),
        "metadata": dict(txn.metadata),
    }


def from_formance(body: Mapping[str, Any], occurred_at: Any = None) -> LedgerTransaction:
    """Read a Formance transaction (request shape) back into a ledger transaction."""
    postings = body.get("postings")
    if not isinstance(postings, list) or not postings:
        raise SchemaMismatchError("Formance transaction needs a non-empty postings list")
    currency: str | None = None
    lines: list[dict[str, Any]] = []
    try:
        for p in postings:
            if not isinstance(p, Mapping):
                raise SchemaMismatchError("posting must be an object")
            asset, amount = p.get("asset"), p.get("amount")
            if not isinstance(asset, str) or not re.fullmatch(r"[A-Z]{3}/[0-9]{1,2}", asset):
                raise SchemaMismatchError(f"asset {str(asset)[:16]!r} is not CCY/PRECISION")
            ccy, prec = asset.split("/")
            if minor_units(ccy) != int(prec):
                raise UnmappedValueError(f"asset {asset} precision differs from ISO 4217")
            if currency not in (None, ccy):
                raise SchemaMismatchError("multi-asset transactions are not supported")
            currency = ccy
            if isinstance(amount, str) and re.fullmatch(r"0|[1-9][0-9]{0,30}", amount):
                amount = int(amount)
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                raise SchemaMismatchError("posting amount must be a positive integer")
            lines += [
                {"account": p.get("source"), "amount_minor": -amount},
                {"account": p.get("destination"), "amount_minor": amount},
            ]
        return LedgerTransaction.model_validate(
            {
                "external_id": body.get("reference"),
                "occurred_at": occurred_at if occurred_at is not None else body.get("timestamp"),
                "currency": currency,
                "lines": lines,
                "metadata": body.get("metadata") or {},
            }
        )
    except ValidationError as e:
        raise SchemaMismatchError(f"not a Formance transaction: {e.error_count()} errors") from None


# --- Modern Treasury (mapping stub) ---------------------------------------------


def to_modern_treasury(
    txn: LedgerTransaction,
    ledger_account_ids: Mapping[str, str],
    *,
    status: Literal["pending", "posted"] = "posted",
) -> dict[str, Any]:
    """A `LedgerTransactionCreateParams` body. Every account needs an MT ledger account id."""
    entries = []
    for line in txn.lines:
        try:
            acct = ledger_account_ids[line.account]
        except KeyError:
            raise UnmappedValueError(
                f"no Modern Treasury ledger account for {line.account}"
            ) from None
        entries.append(
            {
                "direction": "debit" if line.amount_minor > 0 else "credit",
                "ledger_account_id": acct,
                "amount": abs(line.amount_minor),
            }
        )
    out: dict[str, Any] = {
        "ledger_entries": entries,
        "external_id": txn.external_id,
        "effective_at": txn.occurred_at.isoformat(),
        "metadata": dict(txn.metadata),
        "status": status,
    }
    if txn.description is not None:
        out["description"] = txn.description
    return out
