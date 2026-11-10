"""The imported-case store: a SQLite file of redacted, tokenised cases. Append-only.

Separate from the gate database (rules, decisions, audit log): partner data and the
change-control record have different owners and deletion rules (docs/pilot/DATA_CHECKLIST.md),
so `scruple ingest --store` writes its own file and the gate never reads it except
through `load_store`.

What a row holds is a `StoredCase`: the `ExternalCase` (observable facts only, text
already redacted), an optional `CardToken` (never the card number) and a map of
AES-256-GCM envelopes for fields the operator chose to keep encrypted. Every write is
checked by `assert_no_raw_pan`: a Luhn-valid 13-19 digit run anywhere outside the
token and the envelopes refuses the write (`RawPanRefusedError`), whatever the caller
did before.

History is append-only, as everywhere in scruple: database triggers refuse UPDATE and
DELETE. A case imported again with different content (a later export where the
dispute has since been won) is stored as a new version; readers take the latest
version. An identical re-import is a no-op. Deleting pilot data means deleting the
file (DATA_CHECKLIST.md "Deletion at the end").

Webhooks (`write_webhook`). Each accepted Stripe or Adyen delivery is one ingest run,
recorded in `webhook_events` under its event id, so a redelivery (the provider's own
retries, or a replay after a restart) is recognised and stored nothing. Delivery order
is not relied on (ASSUMPTION: a provider may retry or deliver events out of order; not
checked against provider documentation in this build), so every case an event
carries is kept in `webhook_case_events` with the event's own timestamp, and becomes
the case's current version only if no webhook event already applied to that case is
newer. A late, older event is kept (`applied = 0`)
but never overwrites a newer state. Equal timestamps: the later arrival wins.

Retention (`purge`). The one sanctioned delete: a tenant's retention policy
(`scruple.storage.retention`) removes every version of each case opened before a
cutoff, its movements and the webhook bodies that carried it. Run reports, webhook
event ids and payload hashes stay (they hold counts and hashes, no case content, and
keep refusing redeliveries). Each purge appends a `purge:<n>` meta row with the row
counts and a digest of what went, and the caller records the same in the gate's
audit log. The DELETE triggers are dropped and recreated inside the purge's own
transaction, so every other path still finds them.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.ingest.errors import IngestError
from scruple.ingest.models import ExternalCase, IngestBatch, LedgerMovement
from scruple.security.crypto import is_envelope
from scruple.security.pan import DIGIT_RUN, FORMAT_CHARS, CardToken, looks_like_pan

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    label TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    run_id INTEGER NOT NULL REFERENCES ingest_runs(run_id),
    source TEXT NOT NULL,
    row INTEGER NOT NULL,
    body_json TEXT NOT NULL,
    PRIMARY KEY (case_id, version)
);
CREATE TABLE IF NOT EXISTS movements (
    movement_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES ingest_runs(run_id),
    body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS row_errors (
    run_id INTEGER NOT NULL REFERENCES ingest_runs(run_id),
    seq INTEGER NOT NULL,
    row INTEGER NOT NULL,
    col TEXT,
    message TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS webhook_events (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    received_seq INTEGER NOT NULL,
    run_id INTEGER REFERENCES ingest_runs(run_id),
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY (tenant_id, source, event_id)
);
CREATE TABLE IF NOT EXISTS webhook_case_events (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    case_id TEXT NOT NULL,
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_at TEXT NOT NULL,
    applied INTEGER NOT NULL,
    body_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, case_id, source, event_id)
);
"""
_TABLES = (
    "store_meta",
    "ingest_runs",
    "cases",
    "movements",
    "row_errors",
    "webhook_events",
    "webhook_case_events",
)
DEFAULT_TENANT = "default"
_TRIGGERS = "\n".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"""
    for t in _TABLES
)


class StoreError(IngestError):
    """The store file is missing, not a scruple case store, or refused a write."""


class RawPanRefusedError(IngestError):
    """A record about to be stored contains a card number. Nothing was written."""


class StoredCase(BaseModel):
    """One imported case as stored: no card number and no free text in the clear."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case: ExternalCase
    card: CardToken | None = None
    # field name -> `enc:v1:...` envelope, bound to `case:<case_id>:<field>`
    encrypted: dict[str, str] = Field(default_factory=dict)
    row: int = Field(default=0, ge=0)  # input row (or record) number it came from

    def context(self, field: str) -> str:
        return encryption_context(self.case.case_id, field)


def encryption_context(case_id: str, field: str) -> str:
    return f"case:{case_id}:{field}"


def find_pans(text: str) -> int:
    """How many Luhn-valid 13-19 digit runs (with separators) `text` contains."""
    norm = FORMAT_CHARS.sub("", text)
    return sum(1 for m in DIGIT_RUN.finditer(norm) if looks_like_pan(re.sub(r"\D", "", m.group())))


def _clear_parts(sc: StoredCase) -> str:
    """Everything stored in the clear: the record minus the token hash and envelopes
    (hex and base64 can contain digit runs by chance; neither is a card number)."""
    body: dict[str, Any] = sc.model_dump(mode="json", exclude={"encrypted"})
    if body.get("card"):
        body["card"] = {k: v for k, v in body["card"].items() if k != "token"}
    return json.dumps(body, sort_keys=True)


def assert_no_raw_pan(sc: StoredCase) -> None:
    if find_pans(_clear_parts(sc)):
        raise RawPanRefusedError(
            f"record {sc.case.case_id[:64]!r} contains a card number; refused to store it"
        )
    for name, env in sc.encrypted.items():
        if not is_envelope(env):
            raise RawPanRefusedError(f"encrypted field {name!r} is not an envelope")


def _encode(sc: StoredCase) -> str:
    return json.dumps(sc.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class RunCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cases_new: int = 0
    cases_updated: int = 0  # a new version of an already stored case
    cases_unchanged: int = 0
    movements_new: int = 0
    movements_unchanged: int = 0


class WebhookWrite(BaseModel):
    """What storing one webhook event did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    duplicate: bool  # this event id was stored before: nothing was written
    run_id: int | None
    applied: int  # cases that became (or confirmed) the current version
    stale: int  # cases kept but not applied: a newer event was already applied
    counts: RunCounts = RunCounts()


def _utc(at: datetime) -> str:
    """Event time as text that orders lexicographically (UTC, microseconds)."""
    return at.astimezone(UTC).isoformat(timespec="microseconds")


class CaseStore:
    """Open (and create if needed) a case store file. Safe to share across threads
    (the API's request handlers): every transaction holds one lock."""

    def __init__(self, path: str | Path, *, create: bool = True) -> None:
        loc = str(path)
        if loc != ":memory:" and not create and not Path(loc).is_file():
            raise StoreError(f"no case store at {loc}")
        self.path = loc
        self._db = sqlite3.connect(loc, isolation_level=None, check_same_thread=False)
        self._lock = threading.RLock()
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        try:
            existing = {
                r[0]
                for r in self._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        except sqlite3.DatabaseError:
            self._db.close()
            raise StoreError(f"{loc} is not a SQLite file") from None
        if existing and "cases" not in existing:
            self._db.close()
            raise StoreError(f"{loc} is a SQLite file but not a scruple case store")
        self._db.executescript(SCHEMA + _TRIGGERS)
        self._db.execute(
            "INSERT OR IGNORE INTO store_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> CaseStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[None]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # --- writes ------------------------------------------------------------------

    def write_run(
        self,
        *,
        source: str,
        input_sha256: str,
        label: str,
        cases: Sequence[StoredCase],
        movements: Sequence[LedgerMovement],
        errors: Sequence[tuple[int, str | None, str]],
        report: dict[str, Any],
        synthetic: bool = False,
    ) -> tuple[int, RunCounts]:
        """Write one ingest run atomically. Every case is PAN-checked first.

        A store holds either synthetic or real data, never both: the first run sets
        which, and a run of the other kind is refused."""
        _check_pans(cases, movements)
        with self._tx():
            return self._write_run(
                source=source,
                input_sha256=input_sha256,
                label=label,
                cases=cases,
                movements=movements,
                errors=errors,
                report=report,
                synthetic=synthetic,
            )

    def _write_run(
        self,
        *,
        source: str,
        input_sha256: str,
        label: str,
        cases: Sequence[StoredCase],
        movements: Sequence[LedgerMovement],
        errors: Sequence[tuple[int, str | None, str]],
        report: dict[str, Any],
        synthetic: bool,
    ) -> tuple[int, RunCounts]:
        """`write_run` inside the caller's transaction (inputs already PAN-checked)."""
        counts = {k: 0 for k in RunCounts.model_fields}
        kind = "synthetic" if synthetic else "real"
        have = self.kind()
        if have is None:
            self._db.execute("INSERT INTO store_meta(key, value) VALUES ('data', ?)", (kind,))
        elif have != kind:
            raise StoreError(f"this store holds {have} data; refusing to add {kind} data to it")
        row = self._db.execute("SELECT COALESCE(MAX(run_id), 0) + 1 FROM ingest_runs")
        run_id = int(row.fetchone()[0])
        self._db.execute(
            "INSERT INTO ingest_runs(run_id, source, input_sha256, label, report_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, source, input_sha256, label, "{}"),
        )
        for sc in cases:
            body = _encode(sc)
            last = self._db.execute(
                "SELECT version, body_json FROM cases WHERE case_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (sc.case.case_id,),
            ).fetchone()
            if last is not None and _same_case(last["body_json"], body):
                counts["cases_unchanged"] += 1
                continue
            version = 1 if last is None else int(last["version"]) + 1
            counts["cases_new" if last is None else "cases_updated"] += 1
            self._db.execute(
                "INSERT INTO cases(case_id, version, run_id, source, row, body_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sc.case.case_id, version, run_id, sc.case.source, sc.row, body),
            )
        for m in movements:
            mid = f"{m.source}:{m.external_id}"
            body = m.model_dump_json()
            prev = self._db.execute(
                "SELECT body_json FROM movements WHERE movement_id = ?", (mid,)
            ).fetchone()
            if prev is not None:
                if prev["body_json"] != body:
                    raise StoreError(f"movement {mid[:80]} is stored with different content")
                counts["movements_unchanged"] += 1
                continue
            counts["movements_new"] += 1
            self._db.execute(
                "INSERT INTO movements(movement_id, case_id, run_id, body_json) "
                "VALUES (?, ?, ?, ?)",
                (mid, f"{m.source}:{m.case_external_id}", run_id, body),
            )
        for seq, (r, col, msg) in enumerate(errors):
            self._db.execute(
                "INSERT INTO row_errors(run_id, seq, row, col, message) VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, r, col, msg),
            )
        # The run row is append-only, so the report is attached as its own meta row.
        full = dict(report, run_id=run_id, **counts)
        self._db.execute(
            "INSERT INTO store_meta(key, value) VALUES (?, ?)",
            (f"run:{run_id}:report", json.dumps(full, sort_keys=True)),
        )
        return run_id, RunCounts(**counts)

    def write_webhook(
        self,
        *,
        source: str,
        event_id: str,
        event_type: str,
        event_at: datetime,
        payload_sha256: str,
        batch: IngestBatch,
        tenant_id: str = DEFAULT_TENANT,
        synthetic: bool = False,
    ) -> WebhookWrite:
        """Store one accepted webhook event. Idempotent by (tenant, source, event id);
        safe against out-of-order delivery (see the module docstring)."""
        if event_at.tzinfo is None:
            raise StoreError("webhook event time needs a UTC offset")
        cases = [StoredCase(case=c) for c in batch.cases]
        _check_pans(cases, batch.movements)
        at = _utc(event_at)
        with self._tx():
            seen = self._db.execute(
                "SELECT 1 FROM webhook_events WHERE tenant_id=? AND source=? AND event_id=?",
                (tenant_id, source, event_id),
            ).fetchone()
            if seen is not None:
                return WebhookWrite(duplicate=True, run_id=None, applied=0, stale=0)
            applied: list[StoredCase] = []
            stale: list[StoredCase] = []
            for sc in cases:
                newest = self._db.execute(
                    "SELECT MAX(event_at) FROM webhook_case_events "
                    "WHERE tenant_id=? AND case_id=? AND applied=1",
                    (tenant_id, sc.case.case_id),
                ).fetchone()[0]
                (applied if newest is None or at >= newest else stale).append(sc)
            run_id, counts = self._write_run(
                source=f"{source}-webhook",
                input_sha256=payload_sha256,
                label=f"{source} webhook {event_id[:80]}",
                cases=applied,
                movements=batch.movements,
                errors=(),
                report={
                    "event_id": event_id,
                    "event_type": event_type,
                    "event_at": at,
                    "stale_cases": len(stale),
                },
                synthetic=synthetic,
            )
            seq = self._db.execute(
                "SELECT COALESCE(MAX(received_seq), 0) + 1 FROM webhook_events"
            ).fetchone()[0]
            self._db.execute(
                "INSERT INTO webhook_events(tenant_id, source, event_id, event_type, event_at, "
                "received_seq, run_id, payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, source, event_id, event_type, at, int(seq), run_id, payload_sha256),
            )
            for sc in cases:
                self._db.execute(
                    "INSERT INTO webhook_case_events(tenant_id, case_id, source, event_id, "
                    "event_at, applied, body_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        tenant_id,
                        sc.case.case_id,
                        source,
                        event_id,
                        at,
                        int(sc in applied),
                        _encode(sc),
                    ),
                )
        return WebhookWrite(
            duplicate=False, run_id=run_id, applied=len(applied), stale=len(stale), counts=counts
        )

    # --- retention ---------------------------------------------------------------

    PURGED_TABLES = ("cases", "movements", "webhook_case_events")

    def purge(
        self, cutoff: datetime, *, tenant_id: str = DEFAULT_TENANT
    ) -> dict[str, tuple[int, str]]:
        """Delete row-level data of cases opened before `cutoff`. Returns, per table,
        (rows deleted, SHA-256 digest of their keys and body hashes)."""
        import hashlib

        if cutoff.tzinfo is None:
            raise StoreError("retention cutoff needs a UTC offset")

        def sha(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        def digest(keys: list[str]) -> str:
            return sha("\n".join(sorted(keys)))

        out: dict[str, tuple[int, str]] = {}
        with self._tx():
            old: set[str] = set()
            for r in self._db.execute("SELECT case_id, body_json FROM cases"):
                opened = datetime.fromisoformat(json.loads(r["body_json"])["case"]["opened_at"])
                if opened < cutoff:
                    old.add(str(r["case_id"]))
            ids = sorted(old)
            marks = ",".join("?" * len(ids))
            plan = {
                "cases": (
                    f"SELECT case_id || '|' || version AS k, body_json FROM cases "  # noqa: S608
                    f"WHERE case_id IN ({marks})",
                    f"DELETE FROM cases WHERE case_id IN ({marks})",  # noqa: S608
                    tuple(ids),
                ),
                "movements": (
                    f"SELECT movement_id AS k, body_json FROM movements "  # noqa: S608
                    f"WHERE case_id IN ({marks})",
                    f"DELETE FROM movements WHERE case_id IN ({marks})",  # noqa: S608
                    tuple(ids),
                ),
                "webhook_case_events": (
                    "SELECT case_id || '|' || source || '|' || event_id AS k, body_json "  # noqa: S608
                    f"FROM webhook_case_events WHERE tenant_id = ? AND case_id IN ({marks})",
                    f"DELETE FROM webhook_case_events WHERE tenant_id = ? "  # noqa: S608
                    f"AND case_id IN ({marks})",
                    (tenant_id, *ids),
                ),
            }
            for table, (select, delete, args) in plan.items():
                if not ids:
                    out[table] = (0, digest([]))
                    continue
                rows = self._db.execute(select, args).fetchall()
                keys = [f"{r['k']}|{sha(r['body_json'])}" for r in rows]
                self._db.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
                self._db.execute(delete, args)
                self._db.execute(
                    f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                )
                out[table] = (len(rows), digest(keys))
            n = self._db.execute(
                "SELECT COUNT(*) FROM store_meta WHERE key LIKE 'purge:%'"
            ).fetchone()[0]
            self._db.execute(
                "INSERT INTO store_meta(key, value) VALUES (?, ?)",
                (
                    f"purge:{int(n) + 1}",
                    json.dumps(
                        {
                            "tenant_id": tenant_id,
                            "cutoff": _utc(cutoff),
                            "tables": {
                                t: {"rows": c, "digest_sha256": d} for t, (c, d) in out.items()
                            },
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return out

    def purges(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT key, value FROM store_meta WHERE key LIKE 'purge:%'"
        ).fetchall()
        return [
            json.loads(r["value"])
            for r in sorted(rows, key=lambda r: int(str(r["key"]).split(":")[1]))
        ]

    # --- reads -------------------------------------------------------------------

    def webhook_events(self) -> list[dict[str, Any]]:
        """Every stored webhook event (ids, times, hashes; no case content), in arrival
        order."""
        rows = self._db.execute(
            "SELECT tenant_id, source, event_id, event_type, event_at, received_seq, run_id, "
            "payload_sha256 FROM webhook_events ORDER BY received_seq"
        ).fetchall()
        return [dict(r) for r in rows]

    def webhook_case_events(self) -> list[dict[str, Any]]:
        """Every case an event carried, whether it was applied, in event time order."""
        rows = self._db.execute(
            "SELECT tenant_id, case_id, source, event_id, event_at, applied "
            "FROM webhook_case_events ORDER BY case_id, event_at, event_id"
        ).fetchall()
        return [dict(r, applied=bool(r["applied"])) for r in rows]

    def kind(self) -> str | None:
        """ "synthetic" or "real" once a run is stored, else None."""
        row = self._db.execute("SELECT value FROM store_meta WHERE key = 'data'").fetchone()
        return None if row is None else str(row["value"])

    def cases(self) -> list[StoredCase]:
        """Latest version of every case, ordered by case id."""
        rows = self._db.execute(
            "SELECT c.body_json FROM cases c JOIN ("
            " SELECT case_id, MAX(version) AS v FROM cases GROUP BY case_id"
            ") last ON c.case_id = last.case_id AND c.version = last.v ORDER BY c.case_id"
        ).fetchall()
        return [StoredCase.model_validate_json(r["body_json"]) for r in rows]

    def movements(self) -> list[LedgerMovement]:
        rows = self._db.execute("SELECT body_json FROM movements ORDER BY movement_id").fetchall()
        return [LedgerMovement.model_validate_json(r["body_json"]) for r in rows]

    def versions(self) -> dict[str, int]:
        rows = self._db.execute("SELECT case_id, MAX(version) AS v FROM cases GROUP BY case_id")
        return {r["case_id"]: int(r["v"]) for r in rows}

    def runs(self) -> list[dict[str, Any]]:
        out = []
        for r in self._db.execute("SELECT run_id FROM ingest_runs ORDER BY run_id"):
            rep = self._db.execute(
                "SELECT value FROM store_meta WHERE key = ?", (f"run:{r['run_id']}:report",)
            ).fetchone()
            out.append(json.loads(rep["value"]) if rep else {"run_id": r["run_id"]})
        return out

    def row_errors(self, run_id: int | None = None) -> list[tuple[int, int, str | None, str]]:
        q = "SELECT run_id, row, col, message FROM row_errors"
        args: tuple[object, ...] = ()
        if run_id is not None:
            q += " WHERE run_id = ?"
            args = (run_id,)
        rows = self._db.execute(q + " ORDER BY run_id, seq", args).fetchall()
        return [(int(r["run_id"]), int(r["row"]), r["col"], r["message"]) for r in rows]

    def all_text(self) -> Iterator[str]:
        """Every text value in every table, for the raw-PAN scan in `ingest verify`."""
        for t in _TABLES:
            for row in self._db.execute(f"SELECT * FROM {t}"):  # noqa: S608 - fixed names
                for v in tuple(row):
                    if isinstance(v, str):
                        yield v


def _check_pans(cases: Sequence[StoredCase], movements: Sequence[LedgerMovement]) -> None:
    for sc in cases:
        assert_no_raw_pan(sc)
    for m in movements:
        if find_pans(m.model_dump_json()):
            raise RawPanRefusedError(f"movement {m.external_id[:64]!r} contains a card number")


def _same_case(stored: str, new: str) -> bool:
    """Same content, ignoring the input row number and the envelopes' random nonces."""
    a, b = json.loads(stored), json.loads(new)
    for d in (a, b):
        d.pop("row", None)
        d["encrypted"] = sorted(d.get("encrypted", {}))
    return bool(a == b)


__all__ = [
    "CaseStore",
    "RawPanRefusedError",
    "RunCounts",
    "StoreError",
    "StoredCase",
    "WebhookWrite",
    "assert_no_raw_pan",
    "encryption_context",
    "find_pans",
]
