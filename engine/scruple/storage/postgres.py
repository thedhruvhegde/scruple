"""PostgreSQL backend (psycopg 3): the same repositories as SQLite, on Postgres.

Rows are encoded exactly as on SQLite (ISO 8601 timestamps and JSON as text), so a
hash computed before a write matches what is read back on either backend.

Append-only history is enforced in the database: row triggers refuse UPDATE and
DELETE on `audit_events` and `decisions`, refuse changes to rule content (only
`status` and `rollback_to` may change) and refuse DELETE on `rule_versions`;
statement triggers refuse TRUNCATE on all three. The table owner can still drop a
trigger; run the service as a role that does not own the tables (see
docs/ARCHITECTURE.md) and rely on the audit hash chain to detect the rest.

Writers serialise on a transaction-scoped advisory lock, which plays the role of
SQLite's BEGIN IMMEDIATE: the read-then-append steps in the audit log and decision
numbering cannot interleave across processes.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import errors
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from scruple.storage import episodes, imported_shadow, jobs
from scruple.storage.base import DuplicateKeyError, ImmutableHistoryError, StorageError
from scruple.storage.sql import Params, Row, SQLStorage

# Arbitrary constant: the advisory lock id that serialises scruple writers.
WRITE_LOCK_KEY = 0x5C2_0B1E
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_HISTORY_MESSAGES = ("append-only", "immutable")

# Keep in step with the alembic migrations (scruple/storage/migrations); a test
# checks that both produce the same catalog. The DDL is cumulative and idempotent,
# like the migrations: 0001 tables, 0002 api keys, 0003 tenancy (a column added
# to existing tables, then the primary keys widened), 0004 imported shadow, so
# `create_schema=True` also upgrades a database made by an older version.
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

CREATE TABLE IF NOT EXISTS audit_events (
    seq BIGINT PRIMARY KEY,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    body_json TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION scruple_refuse() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '%', TG_ARGV[0];
END
$$;

CREATE OR REPLACE FUNCTION scruple_rule_content_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rule_id IS DISTINCT FROM OLD.rule_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.spec_json IS DISTINCT FROM OLD.spec_json
       OR NEW.owner IS DISTINCT FROM OLD.owner
       OR NEW.provenance_json IS DISTINCT FROM OLD.provenance_json
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'rule content is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE TRIGGER rule_versions_immutable BEFORE UPDATE ON rule_versions
    FOR EACH ROW EXECUTE FUNCTION scruple_rule_content_immutable();
CREATE OR REPLACE TRIGGER rule_versions_no_delete BEFORE DELETE ON rule_versions
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('rule history is immutable');
CREATE OR REPLACE TRIGGER rule_versions_no_truncate BEFORE TRUNCATE ON rule_versions
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('rule history is immutable');

CREATE OR REPLACE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('audit log is append-only');
CREATE OR REPLACE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('audit log is append-only');
CREATE OR REPLACE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('audit log is append-only');

CREATE OR REPLACE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('decisions are append-only');
CREATE OR REPLACE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('decisions are append-only');
CREATE OR REPLACE TRIGGER decisions_no_truncate BEFORE TRUNCATE ON decisions
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('decisions are append-only');

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

CREATE OR REPLACE FUNCTION scruple_api_key_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.key_id IS DISTINCT FROM OLD.key_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.roles_json IS DISTINCT FROM OLD.roles_json
       OR NEW.scopes_json IS DISTINCT FROM OLD.scopes_json
       OR NEW.secret_hash IS DISTINCT FROM OLD.secret_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.description IS DISTINCT FROM OLD.description
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.rate_per_minute IS DISTINCT FROM OLD.rate_per_minute
       OR NEW.burst IS DISTINCT FROM OLD.burst
       OR NEW.max_body_bytes IS DISTINCT FROM OLD.max_body_bytes
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'api key is immutable except last use and one revocation';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE TRIGGER api_keys_immutable BEFORE UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION scruple_api_key_immutable();
CREATE OR REPLACE TRIGGER api_keys_no_delete BEFORE DELETE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('api keys are append-only; revoke instead');
CREATE OR REPLACE TRIGGER api_keys_no_truncate BEFORE TRUNCATE ON api_keys
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('api keys are append-only; revoke instead');

-- tenancy (migration 0003): every history row belongs to a tenant
ALTER TABLE rule_versions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'rule_versions'::regclass AND contype = 'p'
          AND pg_get_constraintdef(oid) LIKE '%tenant_id%'
    ) THEN
        ALTER TABLE rule_versions DROP CONSTRAINT rule_versions_pkey,
            ADD CONSTRAINT rule_versions_pkey PRIMARY KEY (tenant_id, rule_id, version);
        ALTER TABLE audit_events DROP CONSTRAINT audit_events_pkey,
            ADD CONSTRAINT audit_events_pkey PRIMARY KEY (tenant_id, seq);
        ALTER TABLE decisions DROP CONSTRAINT decisions_pkey,
            ADD CONSTRAINT decisions_pkey PRIMARY KEY (tenant_id, decision_id);
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION scruple_rule_content_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rule_id IS DISTINCT FROM OLD.rule_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.spec_json IS DISTINCT FROM OLD.spec_json
       OR NEW.owner IS DISTINCT FROM OLD.owner
       OR NEW.provenance_json IS DISTINCT FROM OLD.provenance_json
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION 'rule content is immutable';
    END IF;
    RETURN NEW;
END
$$;
"""

SCHEMA = BASE_SCHEMA + imported_shadow.POSTGRES_DDL + episodes.POSTGRES_DDL + jobs.POSTGRES_DDL


def translate(exc: psycopg.Error) -> StorageError:
    msg = str(exc).strip()
    if isinstance(exc, errors.RaiseException) and any(m in msg for m in _HISTORY_MESSAGES):
        return ImmutableHistoryError(msg)
    if isinstance(exc, errors.UniqueViolation):
        return DuplicateKeyError(msg)
    return StorageError(msg)


class PostgresConnection:
    """One psycopg connection shared across threads behind a re-entrant lock."""

    def __init__(
        self,
        dsn: str,
        schema: str | None = None,
        create_schema: bool = True,
        *,
        serialise_writes: bool = True,
    ) -> None:
        # serialise_writes=False: no global advisory lock per transaction (the job
        # queue, whose claims rely on row locks and SKIP LOCKED instead).
        self._serialise = serialise_writes
        if schema is not None:
            if not SCHEMA_NAME.match(schema):
                raise ValueError(f"invalid schema name {schema!r}")
            dsn = make_conninfo(dsn, options=f"-c search_path={schema}")
        try:
            self._conn: psycopg.Connection[Row] = psycopg.connect(
                dsn, autocommit=True, row_factory=dict_row
            )
        except psycopg.Error as exc:
            raise translate(exc) from exc
        self._lock = threading.RLock()
        self._depth = 0
        if create_schema:
            with self.transaction():
                if not self._serialise:  # DDL always serialises with other creators
                    self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))
                if schema is not None:
                    self.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                self.execute(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant transaction. Only the outermost level commits or rolls back."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._depth = 1
            try:
                with self._conn.transaction():
                    if self._serialise:
                        self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))
                    yield
            except psycopg.Error as exc:
                raise translate(exc) from exc
            finally:
                self._depth = 0

    @staticmethod
    def _sql(sql: str, params: Params) -> str:
        return sql.replace("?", "%s") if params else sql

    def execute(self, sql: str, params: Params = ()) -> None:
        with self._lock:
            try:
                self._conn.execute(self._sql(sql, params), tuple(params) or None)
            except psycopg.Error as exc:
                raise translate(exc) from exc

    def query(self, sql: str, params: Params = ()) -> list[Row]:
        with self._lock:
            try:
                cur = self._conn.execute(self._sql(sql, params), tuple(params) or None)
                return cur.fetchall()
            except psycopg.Error as exc:  # pragma: no cover - our queries are static
                raise translate(exc) from exc


class PostgresStorage(SQLStorage):
    """`Storage` on PostgreSQL.

    `dsn` is a libpq URL or conninfo string. `schema` (optional) isolates this store
    in its own schema. With `create_schema=False` the tables must already exist,
    for example from `alembic upgrade head`.
    """

    def __init__(self, dsn: str, *, schema: str | None = None, create_schema: bool = True) -> None:
        self.dsn = dsn
        self.schema = schema
        super().__init__(PostgresConnection(dsn, schema, create_schema))


def app_role_grants(role: str, schema: str = "public") -> str:
    """SQL that lets `role` run the service without owning the tables.

    A non-owner cannot drop or disable the append-only triggers, and the grants
    themselves are a second guard: INSERT and SELECT on history, and UPDATE only on
    the two mutable rule columns and the two mutable api key columns. Run it as the
    table owner after migrating.
    """
    for name in (role, schema):
        if not SCHEMA_NAME.match(name):
            raise ValueError(f"invalid identifier {name!r}")
    return f"""
GRANT USAGE ON SCHEMA {schema} TO {role};
REVOKE ALL ON {schema}.rule_versions, {schema}.audit_events, {schema}.decisions,
    {schema}.api_keys FROM {role};
GRANT SELECT, INSERT ON {schema}.rule_versions, {schema}.audit_events, {schema}.decisions,
    {schema}.api_keys TO {role};
GRANT UPDATE (status, rollback_to) ON {schema}.rule_versions TO {role};
GRANT UPDATE (revoked_at, last_used_at) ON {schema}.api_keys TO {role};
GRANT SELECT, INSERT ON {schema}.imported_shadow_events, {schema}.imported_shadow_summaries,
    {schema}.episodes, {schema}.episode_calls, {schema}.job_events TO {role};
GRANT SELECT, INSERT, UPDATE ON {schema}.jobs TO {role};
GRANT USAGE ON SEQUENCE {schema}.jobs_id_seq, {schema}.job_events_id_seq TO {role};
"""
