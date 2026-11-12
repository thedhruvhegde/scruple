"""tenant events: append-only record of tenant administration (scruple.tenancy)

Revision ID: 0015
Revises: 0014

Frozen copy of `scruple.storage.tenants.POSTGRES_DDL` at the time of writing. On
SQLite the table is created on first use instead. Never edit a released migration;
add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS tenant_events (
    tenant_id TEXT NOT NULL,
    number BIGINT NOT NULL,
    kind TEXT NOT NULL,
    body_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, number)
);
CREATE OR REPLACE TRIGGER tenant_events_no_update BEFORE UPDATE ON tenant_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('tenant_events is append-only');
CREATE OR REPLACE TRIGGER tenant_events_no_delete BEFORE DELETE ON tenant_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('tenant_events is append-only');
CREATE OR REPLACE TRIGGER tenant_events_no_truncate BEFORE TRUNCATE ON tenant_events
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('tenant_events is append-only');
"""

DOWNGRADE = """
DROP TABLE IF EXISTS tenant_events;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
