"""billing pushes: append-only ledger of usage sent to a provider (scruple.storage.billing)

Revision ID: 0013
Revises: 0012

Frozen copy of `scruple.storage.billing.POSTGRES_DDL` at the time of writing.
Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS billing_pushes (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT NOT NULL,
    identifier TEXT NOT NULL,
    meter TEXT NOT NULL,
    period_start TEXT NOT NULL,
    delta BIGINT NOT NULL CHECK (delta > 0),
    cumulative BIGINT NOT NULL CHECK (cumulative > 0),
    event_name TEXT NOT NULL,
    customer TEXT NOT NULL,
    livemode INTEGER NOT NULL,
    pushed_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS billing_pushes_identifier
    ON billing_pushes (tenant_id, provider, identifier);
CREATE INDEX IF NOT EXISTS billing_pushes_period
    ON billing_pushes (tenant_id, provider, meter, period_start);

CREATE OR REPLACE TRIGGER billing_pushes_no_update BEFORE UPDATE ON billing_pushes
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('billing_pushes is append-only');
CREATE OR REPLACE TRIGGER billing_pushes_no_delete BEFORE DELETE ON billing_pushes
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('billing_pushes is append-only');
CREATE OR REPLACE TRIGGER billing_pushes_no_truncate BEFORE TRUNCATE ON billing_pushes
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('billing_pushes is append-only');
"""

DOWNGRADE = """
DROP TABLE IF EXISTS billing_pushes;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
