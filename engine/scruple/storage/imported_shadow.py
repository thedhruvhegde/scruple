"""Durable imported-shadow state: accepted batches, append-only, replayed on load.

`scruple.gate.shadow_imported.ImportedShadowRecorder` is pure and in memory. This
module makes it survive a restart without giving up that purity:

- every batch the recorder *accepted* (`record` or `observe` returned) is appended to
  `imported_shadow_events` as its canonical JSON, with a sequence number per
  (tenant, rule, version);
- after each event the summary is appended to `imported_shadow_summaries`, so the
  aggregate at every point is on record even if row-level events are later purged by a
  retention policy (`scruple.storage.retention`);
- a fresh process rebuilds the recorder by replaying the events in order. `record` and
  `observe` are deterministic and all-or-nothing, so the rebuilt state is the state.

Several processes may serve the same rule version (API replicas): every call first
catches up on events written elsewhere, inside the backend's write transaction
(SQLite `BEGIN IMMEDIATE`, the Postgres advisory lock), so two replicas never assign
the same sequence number or score a batch against a stale view.

Both tables are append-only (database triggers), like every other history table.

Purged decisions. Retention deletes row-level events, and with them the case ids a
rebuilt recorder would use to refuse a decision sent twice. So, inside the purge,
the key of every purged decision goes to `imported_shadow_keys`: the SHA-256 of
(tenant, rule, version, case id), never the case id itself. `record` refuses a batch
naming a case whose key is there ("already recorded (purged by retention)"), so a
re-sent decision is not counted twice. The table is append-only and is not purged.

What is stored. Batches come straight from the API or CLI, not through `scruple
ingest`, so the same rules are applied here before a batch is scored or stored: the
kept free-text fields of every case are redacted (`scruple.ingest.pipeline.redact_case`:
`merchant_ref`, `network`, `network_reason_code`, `source_reason`, the shipping
carrier), a case whose id holds a card number or an email address refuses the batch,
and a batch whose stored body would still hold a card number anywhere (a movement
field, an evidence field name) is refused. The recorder scores the redacted batch, so
a replay reproduces exactly what was answered.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict

from scruple.clock import Clock, SystemClock
from scruple.storage.base import DEFAULT_TENANT

if TYPE_CHECKING:
    from scruple.gate.shadow_imported import (
        ImportedBatchResult,
        ImportedDecision,
        ImportedObserveResult,
        ImportedOutcome,
        ImportedShadowRecorder,
        ImportedShadowSummary,
    )
    from scruple.service import AgentGate
    from scruple.storage.sql import Connection

EventKind = Literal["decisions", "outcomes"]

_TABLES = ("imported_shadow_events", "imported_shadow_summaries", "imported_shadow_keys")


def decision_key(tenant_id: str, rule_id: str, version: int, case_id: str) -> str:
    """Stable key of one recorded decision; survives retention as a hash only."""
    return hashlib.sha256(
        f"{tenant_id}\x1f{rule_id}\x1f{version}\x1f{case_id}".encode()
    ).hexdigest()


_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS imported_shadow_keys (
    tenant_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    key_sha256 TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, rule_id, version, key_sha256)
);
"""

SQLITE_DDL = (
    """
CREATE TABLE IF NOT EXISTS imported_shadow_events (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('decisions', 'outcomes')),
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    body_json TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    PRIMARY KEY (tenant_id, rule_id, version, seq)
);
CREATE TABLE IF NOT EXISTS imported_shadow_summaries (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    body_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, rule_id, version, seq)
);
"""
    + _KEYS_DDL
    + "".join(
        f"""CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
"""
        for t in _TABLES
    )
)

# Keep in step with migration 0004 (a test compares the catalogs).
POSTGRES_DDL = (
    """
CREATE TABLE IF NOT EXISTS imported_shadow_events (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('decisions', 'outcomes')),
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    body_json TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    PRIMARY KEY (tenant_id, rule_id, version, seq)
);
CREATE TABLE IF NOT EXISTS imported_shadow_summaries (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    body_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, rule_id, version, seq)
);
"""
    + _KEYS_DDL
    + "".join(
        f"""
CREATE OR REPLACE TRIGGER {t}_no_update BEFORE UPDATE ON {t}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{t} is append-only');
CREATE OR REPLACE TRIGGER {t}_no_delete BEFORE DELETE ON {t}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{t} is append-only');
CREATE OR REPLACE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t}
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('{t} is append-only');
"""
        for t in _TABLES
    )
)


class ShadowEvent(BaseModel):
    """One accepted batch, as stored."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    rule_id: str
    version: int
    seq: int
    kind: EventKind
    at: datetime
    actor: str
    body_json: str
    body_sha256: str


class ShadowSnapshot(BaseModel):
    """The summary right after event `seq` (an aggregate: kept by retention)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    rule_id: str
    version: int
    seq: int
    at: datetime
    body_json: str


@runtime_checkable
class ImportedShadowRepository(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...
    def append_event(self, event: ShadowEvent) -> None: ...
    def events_after(
        self, tenant_id: str, rule_id: str, version: int, after_seq: int
    ) -> list[ShadowEvent]: ...
    def last_seq(self, tenant_id: str, rule_id: str, version: int) -> int: ...
    def first_seq(self, tenant_id: str, rule_id: str, version: int) -> int: ...
    def last_snapshot_seq(self, tenant_id: str, rule_id: str, version: int) -> int: ...
    def append_snapshot(self, snap: ShadowSnapshot) -> None: ...
    def snapshots(self, tenant_id: str, rule_id: str, version: int) -> list[ShadowSnapshot]: ...
    def purged_keys(
        self, tenant_id: str, rule_id: str, version: int, keys: Sequence[str]
    ) -> set[str]: ...


_EVENT_COLS = "tenant_id, rule_id, version, seq, kind, at, actor, body_json, body_sha256"
_SNAP_COLS = "tenant_id, rule_id, version, seq, at, body_json"


class SQLImportedShadow:
    """`ImportedShadowRepository` over a backend `Connection` (SQLite or Postgres)."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def transaction(self) -> AbstractContextManager[None]:
        return self._c.transaction()

    def append_event(self, event: ShadowEvent) -> None:
        with self._c.transaction():
            self._c.execute(
                f"INSERT INTO imported_shadow_events ({_EVENT_COLS}) "  # noqa: S608
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    event.tenant_id,
                    event.rule_id,
                    event.version,
                    event.seq,
                    event.kind,
                    event.at.isoformat(),
                    event.actor,
                    event.body_json,
                    event.body_sha256,
                ),
            )

    def events_after(
        self, tenant_id: str, rule_id: str, version: int, after_seq: int
    ) -> list[ShadowEvent]:
        rows = self._c.query(
            f"SELECT {_EVENT_COLS} FROM imported_shadow_events "  # noqa: S608
            "WHERE tenant_id=? AND rule_id=? AND version=? AND seq>? ORDER BY seq",
            (tenant_id, rule_id, version, after_seq),
        )
        return [ShadowEvent.model_validate(dict(r)) for r in rows]

    def last_seq(self, tenant_id: str, rule_id: str, version: int) -> int:
        rows = self._c.query(
            "SELECT COALESCE(MAX(seq), 0) AS n FROM imported_shadow_events "
            "WHERE tenant_id=? AND rule_id=? AND version=?",
            (tenant_id, rule_id, version),
        )
        return int(rows[0]["n"])

    def first_seq(self, tenant_id: str, rule_id: str, version: int) -> int:
        """The oldest stored event (0 if none). Rises when retention purges events."""
        rows = self._c.query(
            "SELECT COALESCE(MIN(seq), 0) AS n FROM imported_shadow_events "
            "WHERE tenant_id=? AND rule_id=? AND version=?",
            (tenant_id, rule_id, version),
        )
        return int(rows[0]["n"])

    def last_snapshot_seq(self, tenant_id: str, rule_id: str, version: int) -> int:
        """Summaries are never purged, so this is the highest seq ever used."""
        rows = self._c.query(
            "SELECT COALESCE(MAX(seq), 0) AS n FROM imported_shadow_summaries "
            "WHERE tenant_id=? AND rule_id=? AND version=?",
            (tenant_id, rule_id, version),
        )
        return int(rows[0]["n"])

    def append_snapshot(self, snap: ShadowSnapshot) -> None:
        with self._c.transaction():
            self._c.execute(
                f"INSERT INTO imported_shadow_summaries ({_SNAP_COLS}) "  # noqa: S608
                "VALUES (?,?,?,?,?,?)",
                (
                    snap.tenant_id,
                    snap.rule_id,
                    snap.version,
                    snap.seq,
                    snap.at.isoformat(),
                    snap.body_json,
                ),
            )

    def snapshots(self, tenant_id: str, rule_id: str, version: int) -> list[ShadowSnapshot]:
        rows = self._c.query(
            f"SELECT {_SNAP_COLS} FROM imported_shadow_summaries "  # noqa: S608
            "WHERE tenant_id=? AND rule_id=? AND version=? ORDER BY seq",
            (tenant_id, rule_id, version),
        )
        return [ShadowSnapshot.model_validate(dict(r)) for r in rows]

    def purged_keys(
        self, tenant_id: str, rule_id: str, version: int, keys: Sequence[str]
    ) -> set[str]:
        """Which of `keys` belong to decisions retention purged."""
        found: set[str] = set()
        uniq = sorted(set(keys))
        for i in range(0, len(uniq), 500):
            part = uniq[i : i + 500]
            marks = ",".join("?" * len(part))
            head = "SELECT key_sha256 FROM imported_shadow_keys WHERE tenant_id=? AND rule_id=? "
            sql = head + f"AND version=? AND key_sha256 IN ({marks})"
            rows = self._c.query(sql, (tenant_id, rule_id, version, *part))
            found |= {r["key_sha256"] for r in rows}
        return found


def _redacted(
    items: Sequence[ImportedDecision] | Sequence[ImportedOutcome],
) -> list[ImportedDecision] | list[ImportedOutcome]:
    """Each item with its case redacted as ingest redacts it (ValueError on a bad id)."""
    from scruple.ingest.pipeline import redact_case

    out: list[Any] = []
    for item in items:
        case, _ = redact_case(item.case)  # SchemaMismatchError is a ValueError
        out.append(item if case is item.case else item.model_copy(update={"case": case}))
    return out


def _refuse_pans(body: str) -> None:
    from scruple.ingest.store import find_pans

    if find_pans(body):
        raise ValueError("the batch contains a card number; refused to store it")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DurableImportedShadow:
    """An `ImportedShadowRecorder` whose accepted batches are stored and replayed.

    `factory` builds the empty recorder (the candidate is a pure function of an
    immutable rule version, so every process builds the same one). The recorder is
    rebuilt lazily from storage on first use and kept in step on every call.
    """

    def __init__(
        self,
        repo: ImportedShadowRepository,
        rule_id: str,
        version: int,
        factory: Callable[[], ImportedShadowRecorder],
        *,
        tenant_id: str = DEFAULT_TENANT,
        clock: Clock | None = None,
    ) -> None:
        self._repo = repo
        self.rule_id = rule_id
        self.version = version
        self.tenant_id = tenant_id
        self._factory = factory
        self._clock = clock or SystemClock()
        self._rec: ImportedShadowRecorder | None = None
        self._seq = 0
        self._first = 0  # oldest stored event when the recorder was built
        self._dirty = False
        self._lock = threading.RLock()

    # --- replay ----------------------------------------------------------------------

    def _catch_up(self) -> ImportedShadowRecorder:
        first = self._repo.first_seq(self.tenant_id, self.rule_id, self.version)
        if self._rec is not None and first > self._first:
            self._rec = None  # retention purged events this recorder still holds
        if self._rec is None:
            self._rec, self._seq, self._first = self._factory(), 0, first
        rec = self._rec
        try:
            for ev in self._repo.events_after(
                self.tenant_id, self.rule_id, self.version, self._seq
            ):
                _apply(rec, ev)
                self._seq = ev.seq
        except BaseException:
            self._rec = None  # rebuild from scratch next time
            raise
        return rec

    def _append(self, kind: EventKind, actor: str, body: str) -> None:
        assert self._rec is not None
        # Inside the write transaction, after catching up: any summary numbered past
        # what this recorder applied belongs to a purged event. Never reuse a number.
        used = self._repo.last_snapshot_seq(self.tenant_id, self.rule_id, self.version)
        seq = max(self._seq, used) + 1
        at = self._clock.now()
        self._repo.append_event(
            ShadowEvent(
                tenant_id=self.tenant_id,
                rule_id=self.rule_id,
                version=self.version,
                seq=seq,
                kind=kind,
                at=at,
                actor=actor,
                body_json=body,
                body_sha256=_sha(body),
            )
        )
        self._repo.append_snapshot(
            ShadowSnapshot(
                tenant_id=self.tenant_id,
                rule_id=self.rule_id,
                version=self.version,
                seq=seq,
                at=at,
                body_json=self._rec.summary().model_dump_json(),
            )
        )
        self._seq = seq
        if self._first == 0:
            self._first = seq

    # --- the recorder's API, durable -----------------------------------------------

    def record(
        self, decisions: Sequence[ImportedDecision], actor: str = "production"
    ) -> ImportedBatchResult:
        from scruple.gate.shadow_imported import ImportedDecisionBatch

        decisions = cast("list[ImportedDecision]", _redacted(decisions))
        body = ImportedDecisionBatch(decisions=tuple(decisions)).model_dump_json()
        _refuse_pans(body)
        with self._lock, self._writing():
            rec = self._catch_up()
            keys = {
                decision_key(self.tenant_id, self.rule_id, self.version, d.case.case_id): (
                    d.case.case_id
                )
                for d in decisions
            }
            gone = self._repo.purged_keys(self.tenant_id, self.rule_id, self.version, list(keys))
            if gone:
                ids = sorted(keys[k] for k in gone)
                raise ValueError(f"already recorded (purged by retention): {', '.join(ids[:5])}")
            out = rec.record(decisions)  # ValueError: refused, nothing changed
            self._dirty = True
            self._append("decisions", actor, body)
        return out

    def observe(
        self, outcomes: Sequence[ImportedOutcome], actor: str = "production"
    ) -> ImportedObserveResult:
        from scruple.gate.shadow_imported import ImportedOutcomeBatch

        outcomes = cast("list[ImportedOutcome]", _redacted(outcomes))
        body = ImportedOutcomeBatch(outcomes=tuple(outcomes)).model_dump_json()
        _refuse_pans(body)
        with self._lock, self._writing():
            rec = self._catch_up()
            out = rec.observe(outcomes)  # ValueError: refused, nothing changed
            if out.updated:
                self._dirty = True
                self._append("outcomes", actor, body)
        return out

    @contextmanager
    def _writing(self) -> Iterator[None]:
        """One write transaction. If it fails after the in-memory recorder changed
        (the insert or the commit failed), memory is ahead of storage: drop it, so
        the next call rebuilds from what was actually stored."""
        self._dirty = False
        try:
            with self._repo.transaction():
                yield
        except BaseException:
            if self._dirty:
                self._rec = None
            raise
        finally:
            self._dirty = False

    def summary(self) -> ImportedShadowSummary:
        with self._lock:
            return self._catch_up().summary()

    def history(self) -> list[ShadowSnapshot]:
        """Every stored summary, oldest first."""
        return self._repo.snapshots(self.tenant_id, self.rule_id, self.version)

    @property
    def seq(self) -> int:
        """The last event this process has applied."""
        return self._seq


def _apply(rec: ImportedShadowRecorder, ev: ShadowEvent) -> None:
    from scruple.gate.shadow_imported import ImportedDecisionBatch, ImportedOutcomeBatch

    if _sha(ev.body_json) != ev.body_sha256:
        raise ValueError(f"imported shadow event {ev.seq} does not match its hash")
    if ev.kind == "decisions":
        rec.record(ImportedDecisionBatch.model_validate_json(ev.body_json).decisions)
    else:
        rec.observe(ImportedOutcomeBatch.model_validate_json(ev.body_json).outcomes)


class MemoryImportedShadow:
    """`ImportedShadowRepository` in memory, for a `Storage` without the table."""

    def __init__(self) -> None:
        self._events: list[ShadowEvent] = []
        self._snaps: list[ShadowSnapshot] = []
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            yield

    def append_event(self, event: ShadowEvent) -> None:
        key = (event.tenant_id, event.rule_id, event.version, event.seq)
        if any((e.tenant_id, e.rule_id, e.version, e.seq) == key for e in self._events):
            from scruple.storage.base import DuplicateKeyError

            raise DuplicateKeyError(f"imported shadow event {key} exists")
        self._events.append(event)

    def events_after(
        self, tenant_id: str, rule_id: str, version: int, after_seq: int
    ) -> list[ShadowEvent]:
        return sorted(
            (
                e
                for e in self._events
                if (e.tenant_id, e.rule_id, e.version) == (tenant_id, rule_id, version)
                and e.seq > after_seq
            ),
            key=lambda e: e.seq,
        )

    def last_seq(self, tenant_id: str, rule_id: str, version: int) -> int:
        return max((e.seq for e in self.events_after(tenant_id, rule_id, version, 0)), default=0)

    def first_seq(self, tenant_id: str, rule_id: str, version: int) -> int:
        return min((e.seq for e in self.events_after(tenant_id, rule_id, version, 0)), default=0)

    def last_snapshot_seq(self, tenant_id: str, rule_id: str, version: int) -> int:
        return max((s.seq for s in self.snapshots(tenant_id, rule_id, version)), default=0)

    def append_snapshot(self, snap: ShadowSnapshot) -> None:
        self._snaps.append(snap)

    def purged_keys(
        self, tenant_id: str, rule_id: str, version: int, keys: Sequence[str]
    ) -> set[str]:
        return set()  # nothing purges an in-memory repository

    def snapshots(self, tenant_id: str, rule_id: str, version: int) -> list[ShadowSnapshot]:
        return sorted(
            (
                s
                for s in self._snaps
                if (s.tenant_id, s.rule_id, s.version) == (tenant_id, rule_id, version)
            ),
            key=lambda s: s.seq,
        )


def repository_for(storage: object) -> ImportedShadowRepository:
    """The storage's own repository, or an in-memory one if it has none."""
    repo = getattr(storage, "imported_shadow", None)
    return repo if isinstance(repo, ImportedShadowRepository) else MemoryImportedShadow()


def imported_shadows(
    gate: AgentGate, tenant_id: str = DEFAULT_TENANT
) -> Callable[[str, int], DurableImportedShadow]:
    """A cached `(rule_id, version) -> DurableImportedShadow` lookup for one gate.

    The recorder is built from the registry (an unknown rule raises
    `RuleNotFoundError`); its state comes from storage, so a fresh process on the same
    database answers exactly as the one that recorded the batches."""
    repo = repository_for(gate.storage)
    cache: dict[tuple[str, int], DurableImportedShadow] = {}
    lock = threading.Lock()

    def get(rule_id: str, version: int) -> DurableImportedShadow:
        key = (rule_id, version)
        with lock:
            if key not in cache:
                gate.shadow_recorder(rule_id, version)  # validates the version

                def fresh(r: str = rule_id, v: int = version) -> ImportedShadowRecorder:
                    # A new recorder on every rebuild; the candidate is the rule alone.
                    return gate.shadow_recorder(r, v).imported()

                cache[key] = DurableImportedShadow(
                    repo, rule_id, version, fresh, tenant_id=tenant_id, clock=gate.clock
                )
            return cache[key]

    return get


__all__ = [
    "POSTGRES_DDL",
    "SQLITE_DDL",
    "DurableImportedShadow",
    "ImportedShadowRepository",
    "MemoryImportedShadow",
    "SQLImportedShadow",
    "ShadowEvent",
    "ShadowSnapshot",
    "imported_shadows",
    "repository_for",
]
