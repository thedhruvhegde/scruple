"""jobs: the durable job queue and its append-only event history

Revision ID: 0004
Revises: 0003

Frozen copy of `scruple.storage.jobs.POSTGRES_DDL` at the time of writing.
Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY,
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
);
CREATE TABLE IF NOT EXISTS job_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    job_id BIGINT NOT NULL,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    worker TEXT,
    detail_json TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs (tenant_id, idempotency_key);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, run_after);
CREATE INDEX IF NOT EXISTS jobs_rule ON jobs (rule_id, version);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events (job_id);

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

DOWNGRADE = """
DROP TABLE IF EXISTS job_events;
DROP TABLE IF EXISTS jobs;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
