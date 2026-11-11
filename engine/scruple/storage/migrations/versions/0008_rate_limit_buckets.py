"""rate limit buckets: the shared token-bucket store (scruple.auth.ratelimit_db)

Revision ID: 0008
Revises: 0007

Frozen copy of `scruple.storage.ratelimit.POSTGRES_DDL` at the time of writing.
Mutable state, not history: no append-only triggers. Never edit a released
migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    bucket_key TEXT PRIMARY KEY,
    tokens DOUBLE PRECISION NOT NULL,
    at DOUBLE PRECISION NOT NULL,
    full_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_limit_buckets_full ON rate_limit_buckets (full_at);
"""

DOWNGRADE = """
DROP TABLE IF EXISTS rate_limit_buckets;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
