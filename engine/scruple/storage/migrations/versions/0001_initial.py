"""initial schema: rule versions, audit events, decisions, append-only triggers

Revision ID: 0001
Revises:

Frozen copy of the DDL in scruple/storage/postgres.py at the time of writing.
Never edit a released migration; add a new revision. tests/storage/test_migrations.py
checks that `upgrade head` and `PostgresStorage(create_schema=True)` produce the
same catalog.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = """
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
"""

# Dropping history is deliberately possible only by migrating down, as the owner.
DOWNGRADE = """
DROP TABLE IF EXISTS decisions;
DROP TABLE IF EXISTS audit_events;
DROP TABLE IF EXISTS rule_versions;
DROP FUNCTION IF EXISTS scruple_rule_content_immutable();
DROP FUNCTION IF EXISTS scruple_refuse();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
