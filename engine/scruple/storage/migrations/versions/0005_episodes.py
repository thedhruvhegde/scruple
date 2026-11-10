"""episodes: simulator episodes and their call logs, append-only

Revision ID: 0005
Revises: 0004

Frozen copy of `scruple.storage.episodes.POSTGRES_DDL` at the time of writing.
Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS episodes (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    episode_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('simple', 'dispute')),
    seed BIGINT NOT NULL,
    case_type TEXT,
    nonce INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, episode_id)
);
CREATE TABLE IF NOT EXISTS episode_calls (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    episode_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, episode_id, seq)
);

CREATE OR REPLACE TRIGGER episodes_no_update BEFORE UPDATE ON episodes
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('episodes is append-only');
CREATE OR REPLACE TRIGGER episodes_no_delete BEFORE DELETE ON episodes
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('episodes is append-only');
CREATE OR REPLACE TRIGGER episodes_no_truncate BEFORE TRUNCATE ON episodes
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('episodes is append-only');

CREATE OR REPLACE TRIGGER episode_calls_no_update BEFORE UPDATE ON episode_calls
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('episode_calls is append-only');
CREATE OR REPLACE TRIGGER episode_calls_no_delete BEFORE DELETE ON episode_calls
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('episode_calls is append-only');
CREATE OR REPLACE TRIGGER episode_calls_no_truncate BEFORE TRUNCATE ON episode_calls
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('episode_calls is append-only');
"""

DOWNGRADE = """
DROP TABLE IF EXISTS episode_calls;
DROP TABLE IF EXISTS episodes;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
