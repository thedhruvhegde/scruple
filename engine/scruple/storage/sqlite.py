"""SQLite backend: one implementation of the storage interfaces.

Use a file path for local runs, or ":memory:" for tests. Append-only history is
enforced by triggers, so a stray SQL statement also fails.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from scruple.storage import episodes, imported_shadow, jobs, retention
from scruple.storage.base import DuplicateKeyError, ImmutableHistoryError, StorageError
from scruple.storage.sql import Params, Row, SQLStorage

BASE_SCHEMA = """
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

SCHEMA = (
    BASE_SCHEMA
    + imported_shadow.SQLITE_DDL
    + episodes.SQLITE_DDL
    + jobs.SQLITE_DDL
    + retention.SQLITE_DDL
)

_HISTORY_MESSAGES = ("append-only", "immutable")


def translate(exc: sqlite3.Error) -> StorageError:
    msg = str(exc)
    if any(m in msg for m in _HISTORY_MESSAGES):
        return ImmutableHistoryError(msg)
    if isinstance(exc, sqlite3.IntegrityError) and ("UNIQUE" in msg or "PRIMARY KEY" in msg):
        return DuplicateKeyError(msg)
    return StorageError(msg)


class SQLiteConnection:
    """One sqlite3 connection shared across threads behind a re-entrant lock."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant transaction. Only the outermost level commits or rolls back.

        BEGIN IMMEDIATE takes the write lock up front, so two processes on one file
        serialise instead of failing when a read lock is upgraded.
        """
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

    def execute(self, sql: str, params: Params = ()) -> None:
        with self._lock:
            try:
                self._conn.execute(sql, tuple(params))
            except sqlite3.Error as exc:
                raise translate(exc) from exc

    def query(self, sql: str, params: Params = ()) -> list[Row]:
        with self._lock:
            try:
                return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]
            except sqlite3.Error as exc:  # pragma: no cover - our queries are static
                raise translate(exc) from exc


class SQLiteStorage(SQLStorage):
    """`Storage` on SQLite. `path` is a file path or ":memory:"."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(SQLiteConnection(path))
        self.retention = retention.SQLRetention(self.connection, "sqlite")
