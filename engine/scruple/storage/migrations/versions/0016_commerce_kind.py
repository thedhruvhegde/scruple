"""episode kinds: allow commerce episodes (Scruple Guard)

Revision ID: 0016
Revises: 0015

Replaces the `episodes.kind` check from 0010 with the list in
`scruple.storage.episodes.EPISODE_KINDS` at the time of writing (adds `commerce`).
Existing rows all satisfy the new check. Never edit a released migration; add a new
revision.
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE episodes
    DROP CONSTRAINT IF EXISTS episodes_kind_check,
    ADD CONSTRAINT episodes_kind_check CHECK (kind IN (
        'simple', 'dispute', 'scenario', 'ach', 'recon', 'aml', 'payables', 'commerce'));
"""

DOWNGRADE = """
ALTER TABLE episodes
    DROP CONSTRAINT IF EXISTS episodes_kind_check,
    ADD CONSTRAINT episodes_kind_check CHECK (kind IN (
        'simple', 'dispute', 'scenario', 'ach', 'recon', 'aml', 'payables'));
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
