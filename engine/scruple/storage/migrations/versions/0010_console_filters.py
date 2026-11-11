"""console saved filters: per-tenant, append-only (scruple.storage.console_filters)

Revision ID: 0010
Revises: 0009

Frozen copy of `scruple.storage.console_filters.POSTGRES_DDL` at the time of
writing. Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS console_filter_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    filter_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('save', 'delete')),
    list TEXT NOT NULL CHECK (list IN ('versions', 'runs', 'cases', 'approvals')),
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS console_filter_events_tenant
    ON console_filter_events (tenant_id, list, filter_id);
CREATE OR REPLACE TRIGGER console_filter_events_no_update BEFORE UPDATE ON console_filter_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('console_filter_events is append-only');
CREATE OR REPLACE TRIGGER console_filter_events_no_delete BEFORE DELETE ON console_filter_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('console_filter_events is append-only');
CREATE OR REPLACE TRIGGER console_filter_events_no_truncate BEFORE TRUNCATE ON console_filter_events
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('console_filter_events is append-only');
"""

DOWNGRADE = """
DROP TABLE IF EXISTS console_filter_events;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
