"""An imported dispute history: cases plus the ledger postings tied to each case.

Loaders reuse the ingest adapters (`scruple.ingest`), so every input rule there holds
here too: byte and depth limits, strict types, unknown values refused, free text not
kept, test mode only unless `allow_live`. Costs are anchored to ledger postings:
Stripe balance transactions become `LedgerTransaction`s (`from_movement`) and are
posted into an `InMemoryLedger`, which checks balance and idempotency.

Accepted file shapes:

- Stripe: a JSON array of dispute objects, a Stripe list object (`{"object": "list",
  "data": [...]}`), or JSON Lines with one dispute per line. Expand
  `balance_transactions` (they are on the dispute object by default).
- Adyen: one standard notification (`{"live": ..., "notificationItems": [...]}`), a JSON
  array of them, or JSON Lines. Events are folded into one case per payment
  (`originalReference`): first event opens it, the last event gives its state; a case
  that ever reached representment or later counts as contested (ASSUMPTION).
- CSV or JSON Lines with an `ImportConfig` (`scruple.ingest.tabular`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.calibration.facts import CONTEST_STAGES
from scruple.calibration.synthetic import SYNTHETIC_LABEL, SYNTHETIC_METADATA
from scruple.ingest.adyen import ingest_notification
from scruple.ingest.errors import IngestError, MalformedInputError, SchemaMismatchError
from scruple.ingest.ledger import InMemoryLedger, LedgerTransaction, from_movement
from scruple.ingest.limits import MAX_FILE_BYTES, check_size, decode_utf8, load_json
from scruple.ingest.models import ExternalCase, LedgerMovement
from scruple.ingest.stripe.mapping import ingest_dispute
from scruple.ingest.tabular import ImportConfig, import_csv, import_jsonl

Format = Literal["stripe", "adyen", "csv", "jsonl", "records"]
MERCHANT_ACCOUNT = "merchant"
FEE_ACCOUNT = "processor:fees"


class HistoricalCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case: ExternalCase
    ledger: tuple[LedgerTransaction, ...] = ()  # postings tied to this case, by time


class History(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Format
    label: str  # what the data is, shown on every report
    synthetic: bool
    cases: tuple[HistoricalCase, ...]  # by (opened_at, case_id)
    movements: tuple[LedgerMovement, ...] = ()
    skipped: dict[str, int] = Field(
        default_factory=dict
    )  # reason -> count of input records not turned into cases
    merchant_account: str = MERCHANT_ACCOUNT

    def ledger(self) -> InMemoryLedger:
        """Every posting of the history in one ledger (balanced, idempotent)."""
        led = InMemoryLedger()
        for hc in self.cases:
            for txn in hc.ledger:
                led.post(txn)
        return led


def from_records(
    cases: Iterable[ExternalCase],
    movements: Iterable[LedgerMovement] = (),
    *,
    format: Format = "records",
    label: str,
    synthetic: bool = False,
    ledger: Iterable[LedgerTransaction] = (),
    merchant_account: str = MERCHANT_ACCOUNT,
    skipped: Mapping[str, int] | None = None,
) -> History:
    """Build a history. `ledger` adds postings whose `metadata["case"]` names a case."""
    rows = list(cases)
    ids = [c.external_id for c in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate case ids in the history")
    moves = tuple(movements)
    by_case: dict[str, list[LedgerTransaction]] = defaultdict(list)
    for m in moves:
        by_case[m.case_external_id].extend(from_movement(m, merchant_account))
    for txn in ledger:
        case = txn.metadata.get("case")
        if case is not None:
            by_case[case].append(txn)
    unknown = sorted(set(by_case) - set(ids))
    if unknown:
        raise ValueError(f"{len(unknown)} ledger postings name no case in the history")
    out = [
        HistoricalCase(
            case=c,
            ledger=tuple(sorted(by_case.get(c.external_id, ()), key=_txn_key)),
        )
        for c in rows
    ]
    out.sort(key=lambda h: (h.case.opened_at, h.case.case_id))
    hist = History(
        format=format,
        label=label,
        synthetic=synthetic,
        cases=tuple(out),
        movements=moves,
        skipped=dict(sorted((skipped or {}).items())),
        merchant_account=merchant_account,
    )
    hist.ledger()  # raises on unbalanced or conflicting postings
    return hist


def _txn_key(t: LedgerTransaction) -> tuple[str, str]:
    return t.occurred_at.isoformat(), t.external_id


def _documents(data: bytes | str, what: str) -> list[Any]:
    """A JSON array, a list object, a single object, or JSON Lines."""
    check_size(data, MAX_FILE_BYTES, what)
    text = decode_utf8(data, what) if isinstance(data, bytes) else data
    try:
        doc = load_json(text, max_bytes=MAX_FILE_BYTES, what=what)
    except MalformedInputError:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise
        return [load_json(ln, what=f"{what} line {i + 1}") for i, ln in enumerate(lines)]
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict) and doc.get("object") == "list" and isinstance(doc.get("data"), list):
        if doc.get("has_more") is True:
            raise SchemaMismatchError(f"{what}: the list has more pages; export all of them")
        return list(doc["data"])
    return [doc]


def _synthetic_flag(flags: Sequence[bool], what: str) -> bool:
    if any(flags) and not all(flags):
        raise SchemaMismatchError(f"{what} mixes synthetic and real records")
    return bool(flags) and all(flags)


def load_stripe(data: bytes | str, *, label: str, allow_live: bool = False) -> History:
    docs = _documents(data, "Stripe disputes")
    cases: list[ExternalCase] = []
    moves: list[LedgerMovement] = []
    flags: list[bool] = []
    for d in docs:
        if not isinstance(d, dict):
            raise SchemaMismatchError("every Stripe record must be a dispute object")
        meta = d.get("metadata")
        flags.append(
            isinstance(meta, dict) and all(meta.get(k) == v for k, v in SYNTHETIC_METADATA.items())
        )
        batch = ingest_dispute(d, allow_live=allow_live)
        cases.extend(batch.cases)
        moves.extend(batch.movements)
    synthetic = _synthetic_flag(flags, "the Stripe file")
    return from_records(
        cases,
        moves,
        format="stripe",
        label=f"{SYNTHETIC_LABEL}; {label}" if synthetic else label,
        synthetic=synthetic,
    )


def load_adyen(
    data: bytes | str,
    *,
    label: str,
    hmac_keys: Sequence[str] = (),
    verify: bool = True,
    allow_live: bool = False,
) -> History:
    if verify and not hmac_keys:
        raise ValueError("Adyen notifications need an HMAC key (or verify=False)")
    events: list[ExternalCase] = []
    skipped: dict[str, int] = defaultdict(int)
    for doc in _documents(data, "Adyen notifications"):
        if not isinstance(doc, dict):
            raise SchemaMismatchError("every Adyen record must be a notification object")
        res = ingest_notification(doc, hmac_keys, allow_live=allow_live, verify=verify)
        events.extend(res.cases)
        for s in res.skipped:
            skipped[f"adyen: {s.reason}"] += 1
    by_payment: dict[str, list[ExternalCase]] = defaultdict(list)
    for e in events:
        by_payment[e.payment_ref].append(e)
    cases: list[ExternalCase] = []
    for evs in by_payment.values():
        evs.sort(key=lambda e: e.opened_at)  # stable: file order breaks ties
        first, last = evs[0], evs[-1]
        due = next((e.evidence_due_at for e in reversed(evs) if e.evidence_due_at), None)
        cases.append(
            last.model_copy(
                update={
                    "external_id": first.external_id,
                    "opened_at": first.opened_at,
                    "evidence_due_at": due,
                    "evidence_submitted": any(e.stage in CONTEST_STAGES for e in evs),
                    "amount_minor": first.amount_minor,
                    "currency": first.currency,
                }
            )
        )
    if len({c.external_id for c in cases}) != len(cases):
        raise SchemaMismatchError("two Adyen disputes share a first pspReference")
    return from_records(cases, format="adyen", label=label, skipped=skipped)


def load_tabular(
    data: bytes | str, config: ImportConfig, *, label: str, jsonl: bool = False
) -> History:
    report = (import_jsonl if jsonl else import_csv)(data, config)
    skipped = {"row errors": len(report.errors)} if report.errors else {}
    return from_records(
        report.cases, format="jsonl" if jsonl else "csv", label=label, skipped=skipped
    )


__all__ = [
    "FEE_ACCOUNT",
    "MERCHANT_ACCOUNT",
    "HistoricalCase",
    "History",
    "IngestError",
    "from_records",
    "load_adyen",
    "load_stripe",
    "load_tabular",
]
