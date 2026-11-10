"""retention: per-tenant policies, purge records, and the retention-only delete path

Revision ID: 0007
Revises: 0006

Frozen copy of `scruple.storage.retention.POSTGRES_DDL` at the time of writing. The
DELETE trigger on imported_shadow_events is replaced by one that allows a delete only
inside a transaction that set `scruple.retention = 'purge'` (see that module).
Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS retention_policies (
    tenant_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    days INTEGER NOT NULL CHECK (days >= 1),
    set_at TEXT NOT NULL,
    set_by TEXT NOT NULL,
    PRIMARY KEY (tenant_id, seq)
);
CREATE TABLE IF NOT EXISTS retention_purges (
    tenant_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    store TEXT NOT NULL,
    table_name TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    rows_deleted INTEGER NOT NULL,
    digest_sha256 TEXT NOT NULL,
    audit_seq BIGINT,
    PRIMARY KEY (tenant_id, seq)
);

CREATE OR REPLACE TRIGGER retention_policies_no_update BEFORE UPDATE ON retention_policies
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('retention_policies is append-only');
CREATE OR REPLACE TRIGGER retention_policies_no_delete BEFORE DELETE ON retention_policies
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('retention_policies is append-only');
CREATE OR REPLACE TRIGGER retention_policies_no_truncate BEFORE TRUNCATE ON retention_policies
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('retention_policies is append-only');

CREATE OR REPLACE TRIGGER retention_purges_no_update BEFORE UPDATE ON retention_purges
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('retention_purges is append-only');
CREATE OR REPLACE TRIGGER retention_purges_no_delete BEFORE DELETE ON retention_purges
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('retention_purges is append-only');
CREATE OR REPLACE TRIGGER retention_purges_no_truncate BEFORE TRUNCATE ON retention_purges
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('retention_purges is append-only');

CREATE OR REPLACE FUNCTION scruple_refuse_unless_retention() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('scruple.retention', true) = 'purge' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '%', TG_ARGV[0];
END
$$;

CREATE OR REPLACE TRIGGER imported_shadow_events_no_delete BEFORE DELETE
    ON imported_shadow_events FOR EACH ROW
    EXECUTE FUNCTION scruple_refuse_unless_retention('imported_shadow_events is append-only');
"""

DOWNGRADE = """
CREATE OR REPLACE TRIGGER imported_shadow_events_no_delete BEFORE DELETE
    ON imported_shadow_events FOR EACH ROW
    EXECUTE FUNCTION scruple_refuse('imported_shadow_events is append-only');
DROP FUNCTION IF EXISTS scruple_refuse_unless_retention();
DROP TABLE IF EXISTS retention_purges;
DROP TABLE IF EXISTS retention_policies;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
