"""Storage interface and the SQLite implementation.

The `Storage` protocol is the only thing the rest of the package depends on, so a
Postgres implementation can replace SQLiteStorage later. Append-only guarantees are
enforced twice: by the API of this class (no update/delete methods for history) and
by database triggers (so a stray SQL statement also fails). Neither stops someone
with raw file access; the hash chain in scruple.audit detects that case.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Protocol

from scruple.audit.models import AuditEntry
from scruple.domain import RuleSpec
from scruple.registry.models import Provenance, RuleStatus, RuleVersion


class Storage(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...
    def insert_rule_version(self, rv: RuleVersion) -> None: ...
    def update_rule_status(self, rule_id: str, version: int, status: RuleStatus) -> None: ...
    def set_rollback_to(self, rule_id: str, version: int, rollback_to: int | None) -> None: ...
    def get_rule_version(self, rule_id: str, version: int) -> RuleVersion | None: ...
    def list_rule_versions(self, rule_id: str | None = None) -> list[RuleVersion]: ...
    def append_audit(self, entry: AuditEntry) -> None: ...
    def last_audit(self) -> AuditEntry | None: ...
    def list_audit(self) -> list[AuditEntry]: ...
    def insert_decision(self, decision_id: str, rule_id: str, version: int, body: str) -> None: ...
    def get_decision(self, decision_id: str) -> str | None: ...
    def list_decisions(self, rule_id: str | None = None) -> list[str]: ...
    def count_decisions(self) -> int: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    owner TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    rollback_to INTEGER,
    PRIMARY KEY (rule_id, version)
);
CREATE TRIGGER IF NOT EXISTS rule_versions_immutable
BEFORE UPDATE ON rule_versions
WHEN NEW.content_hash IS NOT OLD.content_hash OR NEW.spec_json IS NOT OLD.spec_json
  OR NEW.owner IS NOT OLD.owner OR NEW.provenance_json IS NOT OLD.provenance_json
  OR NEW.created_at IS NOT OLD.created_at OR NEW.rule_id IS NOT OLD.rule_id
  OR NEW.version IS NOT OLD.version
BEGIN SELECT RAISE(ABORT, 'rule content is immutable'); END;
CREATE TRIGGER IF NOT EXISTS rule_versions_no_delete
BEFORE DELETE ON rule_versions
BEGIN SELECT RAISE(ABORT, 'rule history is immutable'); END;

CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    body_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update
BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete
BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
"""


class SQLiteStorage:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant transaction. Only the outermost level commits or rolls back."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._conn.execute("COMMIT")

    def unsafe_execute(self, sql: str) -> None:
        """Raw SQL, for tests and the tamper demo only."""
        with self._lock:
            self._conn.execute(sql)

    # --- rule versions -------------------------------------------------------

    def insert_rule_version(self, rv: RuleVersion) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO rule_versions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rv.rule_id,
                    rv.version,
                    rv.content_hash,
                    rv.spec.model_dump_json(),
                    rv.owner,
                    rv.provenance.model_dump_json(),
                    rv.created_at.isoformat(),
                    rv.status.value,
                    rv.rollback_to,
                ),
            )

    def update_rule_status(self, rule_id: str, version: int, status: RuleStatus) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE rule_versions SET status=? WHERE rule_id=? AND version=?",
                (status.value, rule_id, version),
            )

    def set_rollback_to(self, rule_id: str, version: int, rollback_to: int | None) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE rule_versions SET rollback_to=? WHERE rule_id=? AND version=?",
                (rollback_to, rule_id, version),
            )

    def get_rule_version(self, rule_id: str, version: int) -> RuleVersion | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rule_versions WHERE rule_id=? AND version=?", (rule_id, version)
            ).fetchone()
        return None if row is None else _row_to_rule(row)

    def list_rule_versions(self, rule_id: str | None = None) -> list[RuleVersion]:
        with self._lock:
            if rule_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM rule_versions ORDER BY rule_id, version"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM rule_versions WHERE rule_id=? ORDER BY version", (rule_id,)
                ).fetchall()
        return [_row_to_rule(r) for r in rows]

    # --- audit ---------------------------------------------------------------

    def append_audit(self, entry: AuditEntry) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?)",
                (
                    entry.seq,
                    entry.at.isoformat(),
                    entry.event_type,
                    entry.actor,
                    json.dumps(entry.payload, sort_keys=True),
                    entry.prev_hash,
                    entry.entry_hash,
                ),
            )

    def last_audit(self) -> AuditEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return None if row is None else _row_to_audit(row)

    def list_audit(self) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
        return [_row_to_audit(r) for r in rows]

    # --- decisions -----------------------------------------------------------

    def insert_decision(self, decision_id: str, rule_id: str, version: int, body: str) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO decisions VALUES (?,?,?,?)", (decision_id, rule_id, version, body)
            )

    def get_decision(self, decision_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body_json FROM decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        return None if row is None else str(row["body_json"])

    def list_decisions(self, rule_id: str | None = None) -> list[str]:
        with self._lock:
            if rule_id is None:
                rows = self._conn.execute(
                    "SELECT body_json FROM decisions ORDER BY decision_id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT body_json FROM decisions WHERE rule_id=? ORDER BY decision_id",
                    (rule_id,),
                ).fetchall()
        return [str(r["body_json"]) for r in rows]

    def count_decisions(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()
        return int(row["n"])


def _row_to_rule(row: sqlite3.Row) -> RuleVersion:
    return RuleVersion(
        rule_id=row["rule_id"],
        version=row["version"],
        content_hash=row["content_hash"],
        spec=RuleSpec.model_validate_json(row["spec_json"]),
        owner=row["owner"],
        provenance=Provenance.model_validate_json(row["provenance_json"]),
        status=RuleStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        rollback_to=row["rollback_to"],
    )


def _row_to_audit(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        seq=row["seq"],
        at=datetime.fromisoformat(row["at"]),
        event_type=row["event_type"],
        actor=row["actor"],
        payload=json.loads(row["payload_json"]),
        prev_hash=row["prev_hash"],
        entry_hash=row["entry_hash"],
    )
