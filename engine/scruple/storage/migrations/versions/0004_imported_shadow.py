"""imported shadow: accepted batches and summary snapshots, append-only

Revision ID: 0004
Revises: 0003

Frozen copy of `scruple.storage.imported_shadow.POSTGRES_DDL` at the time of writing.
Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = """
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

CREATE OR REPLACE TRIGGER imported_shadow_events_no_update BEFORE UPDATE
    ON imported_shadow_events FOR EACH ROW
    EXECUTE FUNCTION scruple_refuse('imported_shadow_events is append-only');
CREATE OR REPLACE TRIGGER imported_shadow_events_no_delete BEFORE DELETE
    ON imported_shadow_events FOR EACH ROW
    EXECUTE FUNCTION scruple_refuse('imported_shadow_events is append-only');
CREATE OR REPLACE TRIGGER imported_shadow_events_no_truncate BEFORE TRUNCATE
    ON imported_shadow_events FOR EACH STATEMENT
    EXECUTE FUNCTION scruple_refuse('imported_shadow_events is append-only');

CREATE OR REPLACE TRIGGER imported_shadow_summaries_no_update BEFORE UPDATE
    ON imported_shadow_summaries FOR EACH ROW
    EXECUTE FUNCTION scruple_refuse('imported_shadow_summaries is append-only');
CREATE OR REPLACE TRIGGER imported_shadow_summaries_no_delete BEFORE DELETE
    ON imported_shadow_summaries FOR EACH ROW
    EXECUTE FUNCTION scruple_refuse('imported_shadow_summaries is append-only');
CREATE OR REPLACE TRIGGER imported_shadow_summaries_no_truncate BEFORE TRUNCATE
    ON imported_shadow_summaries FOR EACH STATEMENT
    EXECUTE FUNCTION scruple_refuse('imported_shadow_summaries is append-only');
"""

DOWNGRADE = """
DROP TABLE IF EXISTS imported_shadow_summaries;
DROP TABLE IF EXISTS imported_shadow_events;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
