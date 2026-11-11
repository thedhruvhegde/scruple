"""episode kinds: allow aml and payables episodes

Revision ID: 0010
Revises: 0009

Replaces the `episodes.kind` check from 0005 with the list in
`scruple.storage.episodes.EPISODE_KINDS` at the time of writing. Existing rows all
satisfy the new check. Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE episodes
    DROP CONSTRAINT IF EXISTS episodes_kind_check,
    ADD CONSTRAINT episodes_kind_check CHECK (kind IN (
        'simple', 'dispute', 'scenario', 'ach', 'recon', 'aml', 'payables'));
"""

DOWNGRADE = """
ALTER TABLE episodes
    DROP CONSTRAINT IF EXISTS episodes_kind_check,
    ADD CONSTRAINT episodes_kind_check CHECK (kind IN (
        'simple', 'dispute', 'scenario', 'ach', 'recon'));
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
