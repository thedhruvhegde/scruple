"""Tables for the durable job queue (`scruple.jobs`).

`jobs` is mutable state (a job moves queued -> running -> a terminal status), the
one table here that is updated in place; `job_events` is its append-only history, so
every claim, retry, timeout, cancellation and result is on record.

Timestamps are ISO 8601 UTC text with microseconds (`scruple.jobs.queue.ts`), so they
order lexicographically and compare the same way on both backends.
"""

from __future__ import annotations

_COLS = """
    tenant_id TEXT NOT NULL DEFAULT 'default',
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    timeout_s INTEGER NOT NULL,
    run_after TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    result_json TEXT,
    result_sha256 TEXT,
    error TEXT,
    rule_id TEXT,
    version INTEGER
"""

_EVENT_COLS = """
    tenant_id TEXT NOT NULL DEFAULT 'default',
    job_id BIGINT NOT NULL,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    worker TEXT,
    detail_json TEXT NOT NULL
"""

_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs (tenant_id, idempotency_key);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, run_after);
CREATE INDEX IF NOT EXISTS jobs_rule ON jobs (rule_id, version);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events (job_id);
"""

SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,{_COLS});
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,{_EVENT_COLS});
{_INDEXES}
CREATE TRIGGER IF NOT EXISTS job_events_no_update BEFORE UPDATE ON job_events
BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS job_events_no_delete BEFORE DELETE ON job_events
BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS jobs_no_delete BEFORE DELETE ON jobs
BEGIN SELECT RAISE(ABORT, 'jobs are append-only'); END;
"""

# Keep in step with migration 0004 (a test compares the catalogs).
POSTGRES_DDL = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY,{_COLS});
CREATE TABLE IF NOT EXISTS job_events (
    id BIGSERIAL PRIMARY KEY,{_EVENT_COLS});
{_INDEXES}
CREATE OR REPLACE TRIGGER job_events_no_update BEFORE UPDATE ON job_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('job_events is append-only');
CREATE OR REPLACE TRIGGER job_events_no_delete BEFORE DELETE ON job_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('job_events is append-only');
CREATE OR REPLACE TRIGGER job_events_no_truncate BEFORE TRUNCATE ON job_events
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('job_events is append-only');
CREATE OR REPLACE TRIGGER jobs_no_delete BEFORE DELETE ON jobs
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('jobs are append-only');
CREATE OR REPLACE TRIGGER jobs_no_truncate BEFORE TRUNCATE ON jobs
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('jobs are append-only');
"""

__all__ = ["POSTGRES_DDL", "SQLITE_DDL"]
