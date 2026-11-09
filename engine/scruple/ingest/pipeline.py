"""`scruple ingest`: validate, redact, tokenise, encrypt, then store. One report per run.

Order for every input record (a Stripe dispute object, an Adyen notification, a CSV or
JSON Lines row):

1. Size, depth and encoding limits (`scruple.ingest.limits`), as for every adapter.
2. Sensitive columns come out first (CSV/JSONL): `pan_columns` are tokenised with the
   keyed `PanTokenizer` (first six, last four and an HMAC token are kept, never the
   number) and `encrypt_columns` are sealed with AES-256-GCM bound to the case and the
   column. For Stripe and Adyen, `encrypt_paths` names dotted paths in the raw object to
   keep encrypted. Card numbers inside a value that is encrypted are replaced by
   `[PAN]` first, so not even ciphertext holds one.
3. The adapter maps the record (unknown statuses and reasons are errors, not guesses).
4. Redaction (`scruple.security.redact.redact_text`) of the free-text fields that are
   kept (`merchant_ref`, `network`, `network_reason_code`, `source_reason`, and the
   shipping carrier in `facts`). Identifiers
   (`external_id`, `payment_ref`) cannot be redacted without breaking joins, so an id
   that holds a card number or an email address rejects the row.
5. The store refuses any record whose clear-text part still holds a card number
   (`scruple.ingest.store.assert_no_raw_pan`), whatever happened before.

Every input record ends up stored or as a row error; nothing is dropped silently
(`rows_read == rows_ok + rows_failed`, tested as a property). Card numbers found in
input fields that are not kept are counted (by field name, never by value) in
`pans_in_input_not_stored`.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from scruple.calibration.synthetic import SYNTHETIC_METADATA
from scruple.ingest.adyen import fold_adyen_events, ingest_notification
from scruple.ingest.errors import IngestError, MalformedInputError, SchemaMismatchError
from scruple.ingest.limits import MAX_FILE_BYTES, check_size, decode_utf8, load_json
from scruple.ingest.models import ExternalCase, LedgerMovement
from scruple.ingest.store import (
    CaseStore,
    RawPanRefusedError,
    StoredCase,
    assert_no_raw_pan,
    encryption_context,
    find_pans,
)
from scruple.ingest.stripe.mapping import ingest_dispute
from scruple.ingest.tabular import ImportConfig, RowError, csv_rows, import_rows, jsonl_rows
from scruple.security.crypto import FieldCipher
from scruple.security.pan import CardToken, InvalidPanError, PanTokenizer, scrub_pans
from scruple.security.redact import redact_text

Source = Literal["stripe", "adyen", "csv", "jsonl"]
TEXT_FIELDS = ("merchant_ref", "network", "network_reason_code", "source_reason")
ID_FIELDS = ("external_id", "payment_ref")
_PART = r"[A-Za-z0-9_]{1,64}"
DottedPath = Annotated[str, StringConstraints(pattern=rf"^{_PART}(?:\.{_PART}){{0,7}}$")]


class IngestOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Source
    label: str = Field(default="", max_length=200)
    import_config: ImportConfig | None = None  # csv and jsonl
    pan_columns: tuple[str, ...] = ()  # csv/jsonl: card numbers, tokenised
    encrypt_columns: tuple[str, ...] = ()  # csv/jsonl: kept only encrypted
    encrypt_paths: tuple[DottedPath, ...] = ()  # stripe/adyen: dotted paths kept encrypted
    allow_live: bool = False
    verify_adyen: bool = True
    strict: bool = False  # store nothing if any record fails

    @model_validator(mode="after")
    def _check(self) -> IngestOptions:
        tabular = self.source in ("csv", "jsonl")
        if tabular and self.import_config is None:
            raise ValueError(f"{self.source} ingest needs an ImportConfig")
        if not tabular and (self.pan_columns or self.encrypt_columns):
            raise ValueError("pan_columns and encrypt_columns are for csv and jsonl")
        if tabular and self.encrypt_paths:
            raise ValueError("encrypt_paths is for stripe and adyen; use encrypt_columns")
        if len(self.pan_columns) > 1:
            raise ValueError("at most one card-number column")
        if set(self.pan_columns) & set(self.encrypt_columns):
            raise ValueError("a column cannot be both tokenised and encrypted")
        if self.import_config is not None:
            mapped = set(self.import_config.columns.used().values())
            clash = mapped & (set(self.pan_columns) | set(self.encrypt_columns))
            if clash:
                raise ValueError(f"mapped columns cannot be tokenised or encrypted: {clash}")
        return self


class IngestReport(BaseModel):
    """What one run did. Counts and field names only: no values from the input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Source
    label: str
    input_sha256: str
    synthetic: bool
    stored: bool  # False: dry run, or strict mode with errors
    run_id: int | None = None
    rows_read: int
    rows_ok: int
    rows_failed: int
    cases: int
    movements: int
    cases_new: int = 0
    cases_updated: int = 0
    cases_unchanged: int = 0
    movements_new: int = 0
    errors: tuple[RowError, ...] = ()
    truncated_errors: bool = False
    skipped: dict[str, int] = Field(default_factory=dict)
    redactions: dict[str, int] = Field(default_factory=dict)  # placeholder kind -> count
    pans_tokenised: int = 0
    pans_in_input_not_stored: dict[str, int] = Field(default_factory=dict)  # field -> count
    fields_encrypted: int = 0

    def summary(self) -> str:
        where = f"run {self.run_id}" if self.run_id is not None else "nothing stored"
        tag = " [SYNTHETIC DATA]" if self.synthetic else ""
        return (
            f"{self.source}{tag}: {self.rows_read} records read, {self.rows_ok} ok, "
            f"{self.rows_failed} failed; {self.cases} cases ({self.cases_new} new, "
            f"{self.cases_updated} updated, {self.cases_unchanged} unchanged), "
            f"{self.movements} movements; {self.pans_tokenised} card numbers tokenised, "
            f"{self.fields_encrypted} fields encrypted, "
            f"{sum(self.redactions.values())} redactions; {where}"
        )


class _Acc:
    def __init__(self, max_errors: int) -> None:
        self.cases: list[StoredCase] = []
        self.movements: list[LedgerMovement] = []
        self.errors: list[RowError] = []
        self.failed_rows: set[int] = set()
        self.rows = 0
        self.max_errors = max_errors
        self.truncated = False
        self.redactions: Counter[str] = Counter()
        self.pans_seen: Counter[str] = Counter()
        self.tokenised = 0
        self.encrypted = 0
        self.skipped: Counter[str] = Counter()
        self.synthetic: list[bool] = []

    def fail(self, row: int, column: str | None, message: str) -> None:
        self.failed_rows.add(row)
        if len(self.errors) >= self.max_errors:
            self.truncated = True
            return
        self.errors.append(RowError(row=row, column=column, message=message[:300]))


def _walk(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _walk(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, f"{prefix}[]")
    else:
        yield prefix, obj


def _count_pans(obj: Any, acc: _Acc) -> None:
    for path, v in _walk(obj):
        if isinstance(v, str | int) and not isinstance(v, bool) and find_pans(str(v)):
            acc.pans_seen[path[:120]] += 1


def _get_path(obj: Mapping[str, Any], path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


def _redact_case(case: ExternalCase, acc: _Acc) -> ExternalCase:
    for f in ID_FIELDS:
        found = redact_text(getattr(case, f)).counts()
        bad = sorted(k for k in found if k in ("PAN", "EMAIL"))
        if bad:
            raise SchemaMismatchError(
                f"{f} holds {'/'.join(bad).lower()} data; ids cannot be redacted"
            )
    update: dict[str, Any] = {}
    for f in TEXT_FIELDS:
        v = getattr(case, f)
        if not v:
            continue
        r = redact_text(v)
        if r.text != v:
            update[f] = r.text
            acc.redactions.update(r.counts())
    if case.facts is not None and case.facts.shipping_carrier:
        r = redact_text(case.facts.shipping_carrier)
        if r.text != case.facts.shipping_carrier:
            acc.redactions.update(r.counts())
            update["facts"] = case.facts.model_copy(update={"shipping_carrier": r.text})
    if not update:
        return case
    return case.model_copy(update=update)


def _seal(
    case_id: str, values: Mapping[str, str], cipher: FieldCipher | None, acc: _Acc
) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, v in sorted(values.items()):
        if not v:
            continue
        if cipher is None:
            raise IngestError(
                "encrypted fields need a field key (SCRUPLE_FIELD_KEYS); nothing was stored"
            )
        out[name] = cipher.encrypt(scrub_pans(v), encryption_context(case_id, name))
        acc.encrypted += 1
    return out


def _token(raw: str, tokenizer: PanTokenizer | None) -> CardToken | None:
    if not raw.strip():
        return None
    if tokenizer is None:
        raise IngestError(
            "a card-number column needs a tokenisation key (SCRUPLE_TOKEN_KEYS); nothing was stored"
        )
    return tokenizer.tokenize(raw)


def _finish(
    case: ExternalCase,
    acc: _Acc,
    row: int,
    *,
    card: CardToken | None = None,
    sealed: dict[str, str] | None = None,
) -> StoredCase:
    sc = StoredCase(case=_redact_case(case, acc), card=card, encrypted=sealed or {}, row=row)
    assert_no_raw_pan(sc)
    return sc


def _documents(data: bytes | str, what: str) -> list[tuple[int, Any]]:
    """(record number, parsed JSON) for an array, a list object, one object or JSON Lines.
    A bad JSON line becomes a `MalformedInputError` value for that record."""
    check_size(data, MAX_FILE_BYTES, what)
    text = decode_utf8(data, what) if isinstance(data, bytes) else data
    try:
        doc = load_json(text, max_bytes=MAX_FILE_BYTES, what=what)
    except MalformedInputError:
        lines = [(i + 1, ln) for i, ln in enumerate(text.splitlines()) if ln.strip()]
        if len(lines) < 2:
            raise
        out: list[tuple[int, Any]] = []
        for n, ln in lines:
            try:
                out.append((n, load_json(ln, what=f"{what} line {n}")))
            except IngestError as e:
                out.append((n, e))
        return out
    if isinstance(doc, list):
        return list(enumerate(doc, start=1))
    if isinstance(doc, dict) and doc.get("object") == "list" and isinstance(doc.get("data"), list):
        if doc.get("has_more") is True:
            raise SchemaMismatchError(f"{what}: the list has more pages; export all of them")
        return list(enumerate(doc["data"], start=1))
    return [(1, doc)]


def _is_synthetic(doc: Any) -> bool:
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    return isinstance(meta, dict) and all(meta.get(k) == v for k, v in SYNTHETIC_METADATA.items())


def _stripe(data: bytes | str, opts: IngestOptions, cipher: FieldCipher | None, acc: _Acc) -> None:
    for n, doc in _documents(data, "Stripe disputes"):
        acc.rows += 1
        if isinstance(doc, IngestError):
            acc.fail(n, None, str(doc))
            continue
        if not isinstance(doc, dict):
            acc.fail(n, None, "record is not a JSON object")
            continue
        _count_pans(doc, acc)
        try:
            batch = ingest_dispute(doc, allow_live=opts.allow_live)
            case = batch.cases[0]
            sealed = _seal(
                case.case_id,
                {p: _str_at(doc, p) for p in opts.encrypt_paths},
                cipher,
                acc,
            )
            sc = _finish(case, acc, n, sealed=sealed)
        except IngestError as e:
            acc.fail(n, None, str(e))
            continue
        acc.synthetic.append(_is_synthetic(doc))
        acc.cases.append(sc)
        acc.movements.extend(batch.movements)


def _str_at(doc: Mapping[str, Any], path: str) -> str:
    v = _get_path(doc, path)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise SchemaMismatchError(f"{path} is not a string; only strings can be encrypted")
    return v


def _adyen(
    data: bytes | str,
    opts: IngestOptions,
    cipher: FieldCipher | None,
    hmac_keys: Sequence[str],
    acc: _Acc,
) -> None:
    if opts.verify_adyen and not hmac_keys:
        raise IngestError("Adyen notifications need an HMAC key (SCRUPLE_ADYEN_HMAC_KEYS)")
    events: list[tuple[int, ExternalCase]] = []
    secrets: dict[str, dict[str, str]] = {}
    for n, doc in _documents(data, "Adyen notifications"):
        acc.rows += 1
        if isinstance(doc, IngestError):
            acc.fail(n, None, str(doc))
            continue
        if not isinstance(doc, dict):
            acc.fail(n, None, "record is not a JSON object")
            continue
        _count_pans(doc, acc)
        try:
            res = ingest_notification(
                doc, hmac_keys, allow_live=opts.allow_live, verify=opts.verify_adyen
            )
            secret: dict[str, dict[str, str]] = {}
            for it in doc.get("notificationItems") or []:
                inner = it.get("NotificationRequestItem") if isinstance(it, dict) else None
                if not isinstance(inner, dict) or not opts.encrypt_paths:
                    continue
                ref = inner.get("originalReference") or inner.get("pspReference")
                vals = {p: _str_at(inner, p) for p in opts.encrypt_paths}
                secret.setdefault(str(ref), {}).update({k: v for k, v in vals.items() if v})
        except IngestError as e:
            acc.fail(n, None, str(e))
            continue
        for sk in res.skipped:
            acc.skipped[f"adyen: {sk.reason}"] += 1
        events.extend((n, c) for c in res.cases)
        for ref, vals in secret.items():
            secrets.setdefault(ref, {}).update(vals)
        acc.synthetic.append(False)
    try:
        folded = fold_adyen_events([c for _, c in events])
    except IngestError as e:
        for n, _ in events:
            acc.fail(n, None, str(e))
        return
    first_row: dict[str, int] = {}
    for n, c in events:
        first_row.setdefault(c.payment_ref, n)
    for case in folded:
        n = first_row[case.payment_ref]
        try:
            sealed = _seal(case.case_id, secrets.get(case.payment_ref, {}), cipher, acc)
            acc.cases.append(_finish(case, acc, n, sealed=sealed))
        except IngestError as e:
            acc.fail(n, None, str(e))


def _tabular(
    data: bytes | str,
    opts: IngestOptions,
    tokenizer: PanTokenizer | None,
    cipher: FieldCipher | None,
    acc: _Acc,
) -> None:
    cfg = opts.import_config
    assert cfg is not None  # checked by IngestOptions
    header, rows = (csv_rows if opts.source == "csv" else jsonl_rows)(data, cfg)
    pulled = set(opts.pan_columns) | set(opts.encrypt_columns)
    missing = sorted(pulled - set(header))
    if missing:
        raise SchemaMismatchError(
            f"columns named for tokenising or encryption are missing: {missing}"
        )
    id_col = cfg.columns.external_id
    side: dict[int, tuple[dict[str, str], dict[str, str]]] = {}
    clean_rows: list[tuple[int, dict[str, str] | RowError]] = []
    row_of: dict[str, int] = {}
    for n, rec in rows:
        acc.rows += 1
        if isinstance(rec, RowError):
            clean_rows.append((n, rec))
            continue
        _count_pans({k: v for k, v in rec.items() if k not in pulled}, acc)
        pans = {c: rec[c] for c in opts.pan_columns}
        secrets = {c: rec[c] for c in opts.encrypt_columns}
        side[n] = (pans, secrets)
        clean_rows.append((n, {k: v for k, v in rec.items() if k not in pulled}))
        row_of.setdefault(rec.get(id_col, "").strip(), n)
    report = import_rows(cfg, [h for h in header if h not in pulled], clean_rows)
    seen_rows = [n for n, _ in clean_rows]
    for e in report.errors:
        acc.fail(e.row, e.column, e.message)
    for case in report.cases:
        n = row_of[case.external_id]
        pans, secrets = side[n]
        try:
            card = next((_token(v, tokenizer) for v in pans.values()), None)
            if card is not None:
                acc.tokenised += 1
            sealed = _seal(case.case_id, secrets, cipher, acc)
            acc.cases.append(_finish(case, acc, n, card=card, sealed=sealed))
        except InvalidPanError as e:
            acc.fail(n, opts.pan_columns[0], str(e))
        except IngestError as e:
            acc.fail(n, None, str(e))
    # Rows past the error cap are not listed but still count as failed.
    acc.failed_rows |= set(seen_rows) - {sc.row for sc in acc.cases}
    acc.synthetic.append(False)


def ingest(
    data: bytes | str,
    opts: IngestOptions,
    store: CaseStore | None = None,
    *,
    tokenizer: PanTokenizer | None = None,
    cipher: FieldCipher | None = None,
    adyen_hmac_keys: Sequence[str] = (),
    max_errors: int = 10_000,
) -> IngestReport:
    """Run one ingest. With `store=None` it is a dry run (validates, writes nothing).

    Raises `IngestError` only for whole-file problems (bad encoding, too large, a
    missing key); record-level problems are row errors in the report."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    sha = hashlib.sha256(raw).hexdigest()
    acc = _Acc(max_errors)
    if opts.source == "stripe":
        _stripe(data, opts, cipher, acc)
    elif opts.source == "adyen":
        _adyen(data, opts, cipher, adyen_hmac_keys, acc)
    else:
        _tabular(data, opts, tokenizer, cipher, acc)
    failed = acc.failed_rows
    rows_failed = len(failed)
    cases = [sc for sc in acc.cases if sc.row not in failed]
    case_ids = {sc.case.case_id for sc in cases}
    moves = [m for m in acc.movements if f"{m.source}:{m.case_external_id}" in case_ids]
    synthetic = bool(acc.synthetic) and all(acc.synthetic)
    if any(acc.synthetic) and not synthetic:
        raise SchemaMismatchError("the file mixes synthetic and real records")
    base: dict[str, Any] = {
        "source": opts.source,
        "label": opts.label,
        "input_sha256": sha,
        "synthetic": synthetic,
        "rows_read": acc.rows,
        "rows_ok": acc.rows - rows_failed,
        "rows_failed": rows_failed,
        "cases": len(cases),
        "movements": len(moves),
        "errors": tuple(acc.errors),
        "truncated_errors": acc.truncated,
        "skipped": dict(sorted(acc.skipped.items())),
        "redactions": dict(sorted(acc.redactions.items())),
        "pans_tokenised": sum(1 for sc in cases if sc.card is not None),
        "pans_in_input_not_stored": dict(sorted(acc.pans_seen.items())),
        "fields_encrypted": sum(len(sc.encrypted) for sc in cases),
    }
    write = store is not None and not (opts.strict and rows_failed)
    if not write:
        return IngestReport(stored=False, **base)
    assert store is not None
    pre = IngestReport(stored=True, **base)
    run_id, counts = store.write_run(
        source=opts.source,
        input_sha256=sha,
        label=opts.label,
        cases=cases,
        movements=moves,
        errors=[(e.row, e.column, e.message) for e in acc.errors],
        report=pre.model_dump(mode="json", exclude={"errors"}),
        synthetic=synthetic,
    )
    return pre.model_copy(
        update={
            "run_id": run_id,
            "cases_new": counts.cases_new,
            "cases_updated": counts.cases_updated,
            "cases_unchanged": counts.cases_unchanged,
            "movements_new": counts.movements_new,
        }
    )


def stored_cases(cases: Iterable[StoredCase]) -> list[ExternalCase]:
    return [sc.case for sc in cases]


__all__ = [
    "IngestOptions",
    "IngestReport",
    "RawPanRefusedError",
    "ingest",
    "stored_cases",
]
