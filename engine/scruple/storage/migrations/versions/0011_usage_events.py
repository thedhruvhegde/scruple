"""usage events: append-only metering per tenant (scruple.storage.usage)

Revision ID: 0011
Revises: 0010

Frozen copy of `scruple.storage.usage.POSTGRES_DDL` at the time of writing.
Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS usage_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    event_key TEXT NOT NULL,
    meter TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity >= 0),
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    rule_id TEXT,
    version INTEGER,
    recorded_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS usage_events_key ON usage_events (tenant_id, event_key);
CREATE INDEX IF NOT EXISTS usage_events_at ON usage_events (tenant_id, at);

CREATE OR REPLACE TRIGGER usage_events_no_update BEFORE UPDATE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('usage_events is append-only');
CREATE OR REPLACE TRIGGER usage_events_no_delete BEFORE DELETE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('usage_events is append-only');
CREATE OR REPLACE TRIGGER usage_events_no_truncate BEFORE TRUNCATE ON usage_events
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('usage_events is append-only');
"""

DOWNGRADE = """
DROP TABLE IF EXISTS usage_events;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
