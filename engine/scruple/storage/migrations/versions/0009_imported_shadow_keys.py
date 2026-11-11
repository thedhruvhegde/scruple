"""imported shadow keys: hashed keys of decisions purged by retention

Revision ID: 0009
Revises: 0008

Frozen copy of the `imported_shadow_keys` part of
`scruple.storage.imported_shadow.POSTGRES_DDL` at the time of writing. Append-only,
and never purged: a decision re-sent for a purged case is refused by its key hash.
Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

T = "imported_shadow_keys"
UPGRADE = f"""
CREATE TABLE IF NOT EXISTS {T} (
    tenant_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    key_sha256 TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, rule_id, version, key_sha256)
);
CREATE OR REPLACE TRIGGER {T}_no_update BEFORE UPDATE ON {T}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{T} is append-only');
CREATE OR REPLACE TRIGGER {T}_no_delete BEFORE DELETE ON {T}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{T} is append-only');
CREATE OR REPLACE TRIGGER {T}_no_truncate BEFORE TRUNCATE ON {T}
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('{T} is append-only');
"""

DOWNGRADE = f"""
DROP TABLE IF EXISTS {T};
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
