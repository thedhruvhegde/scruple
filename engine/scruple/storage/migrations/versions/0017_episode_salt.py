"""episodes: a per-episode salt column (round 7, G-23)

Revision ID: 0017
Revises: 0016

The database-backed episode store (the API and `scruple mcp sim --db`) keys every new
episode's case with a fresh salt, stored here so a replay in another process rebuilds
the same case; without it a rehearsed (kind, seed) was the same case on every start.
Rows written before this revision have NULL and replay unkeyed, as they were played.
Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

UPGRADE = "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS salt TEXT;"
DOWNGRADE = "ALTER TABLE episodes DROP COLUMN IF EXISTS salt;"


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
