"""SQLite backend: one implementation of the storage interfaces.

Use a file path for local runs, or ":memory:" for tests. Append-only history is
enforced by triggers, so a stray SQL statement also fails.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from scruple.storage import (
    billing,
    console_filters,
    episodes,
    imported_shadow,
    jobs,
    ratelimit,
    retention,
    usage,
)
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
    tenant_id TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY (tenant_id, rule_id, version)
);
CREATE TRIGGER IF NOT EXISTS rule_versions_immutable
BEFORE UPDATE ON rule_versions
WHEN NEW.content_hash IS NOT OLD.content_hash OR NEW.spec_json IS NOT OLD.spec_json
  OR NEW.owner IS NOT OLD.owner OR NEW.provenance_json IS NOT OLD.provenance_json
  OR NEW.created_at IS NOT OLD.created_at OR NEW.rule_id IS NOT OLD.rule_id
  OR NEW.version IS NOT OLD.version OR NEW.tenant_id IS NOT OLD.tenant_id
BEGIN SELECT RAISE(ABORT, 'rule content is immutable'); END;
CREATE TRIGGER IF NOT EXISTS rule_versions_no_delete
BEFORE DELETE ON rule_versions
BEGIN SELECT RAISE(ABORT, 'rule history is immutable'); END;

CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY (tenant_id, seq)
);
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    body_json TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY (tenant_id, decision_id)
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update
BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete
BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;

CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    roles_json TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    description TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT,
    rate_per_minute INTEGER,
    burst INTEGER,
    max_body_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS api_keys_tenant ON api_keys (tenant_id, created_at);
CREATE TRIGGER IF NOT EXISTS api_keys_immutable
BEFORE UPDATE ON api_keys
WHEN NEW.key_id IS NOT OLD.key_id OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.subject IS NOT OLD.subject OR NEW.roles_json IS NOT OLD.roles_json
  OR NEW.scopes_json IS NOT OLD.scopes_json OR NEW.secret_hash IS NOT OLD.secret_hash
  OR NEW.created_at IS NOT OLD.created_at OR NEW.created_by IS NOT OLD.created_by
  OR NEW.description IS NOT OLD.description OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.rate_per_minute IS NOT OLD.rate_per_minute OR NEW.burst IS NOT OLD.burst
  OR NEW.max_body_bytes IS NOT OLD.max_body_bytes
  OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN SELECT RAISE(ABORT, 'api key is immutable except last use and one revocation'); END;
CREATE TRIGGER IF NOT EXISTS api_keys_no_delete
BEFORE DELETE ON api_keys
BEGIN SELECT RAISE(ABORT, 'api keys are append-only; revoke instead'); END;
"""

SCHEMA = (
    BASE_SCHEMA
    + imported_shadow.SQLITE_DDL
    + episodes.SQLITE_DDL
    + jobs.SQLITE_DDL
    + retention.SQLITE_DDL
    + ratelimit.SQLITE_DDL
    + console_filters.SQLITE_DDL
    + usage.SQLITE_DDL
    + billing.SQLITE_DDL
)

_HISTORY_MESSAGES = ("append-only", "immutable")

# Files written before tenancy have the three history tables without `tenant_id`.
# They are rebuilt once, in one transaction, with every row in the default tenant:
# the rows (and so every hash) are copied unchanged. Triggers are dropped first and
# recreated by SCHEMA, so nothing is updated or deleted through them.
_LEGACY_TABLES = ("rule_versions", "audit_events", "decisions")
_LEGACY_TRIGGERS = (
    "rule_versions_immutable",
    "rule_versions_no_delete",
    "audit_no_update",
    "audit_no_delete",
    "decisions_no_update",
    "decisions_no_delete",
)


def _needs_tenancy_upgrade(conn: sqlite3.Connection) -> bool:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(rule_versions)").fetchall()]
    return bool(cols) and "tenant_id" not in cols


def _tenancy_upgrade_script() -> str:
    parts = ["BEGIN IMMEDIATE;"]
    parts += [f"DROP TRIGGER IF EXISTS {t};" for t in _LEGACY_TRIGGERS]
    parts += [f"ALTER TABLE {t} RENAME TO {t}_pre_tenancy;" for t in _LEGACY_TABLES]
    parts.append(SCHEMA)
    for t in _LEGACY_TABLES:
        # table names are the constants above, never input
        parts.append(f"INSERT INTO {t} SELECT *, 'default' FROM {t}_pre_tenancy;")  # noqa: S608
        parts.append(f"DROP TABLE {t}_pre_tenancy;")
    parts.append("COMMIT;")
    return "\n".join(parts)


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
        if _needs_tenancy_upgrade(self._conn):
            self._conn.executescript(_tenancy_upgrade_script())
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
