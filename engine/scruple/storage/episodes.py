"""Tables for simulator episodes kept in the gate database (`scruple.agents.store_db`).

The same record as the CLI's JSON file store (`scruple.agents.store`): an episode is
its kind, seed, case type and nonce plus the ordered log of tool calls, and it is
rebuilt by replaying that log (episodes are deterministic). Both tables are
append-only: a call is never edited or removed, only added with the next `seq`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from scruple.storage.sql import Connection

_TABLES = ("episodes", "episode_calls")

_DDL = """
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
"""

SQLITE_DDL = _DDL + "".join(
    f"""CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
"""
    for t in _TABLES
)

# Keep in step with migration 0005 (a test compares the catalogs).
POSTGRES_DDL = _DDL + "".join(
    f"""
CREATE OR REPLACE TRIGGER {t}_no_update BEFORE UPDATE ON {t}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{t} is append-only');
CREATE OR REPLACE TRIGGER {t}_no_delete BEFORE DELETE ON {t}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{t} is append-only');
CREATE OR REPLACE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t}
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('{t} is append-only');
"""
    for t in _TABLES
)


class EpisodeRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    episode_id: str
    kind: str
    seed: int
    case_type: str | None
    nonce: int
    created_at: datetime


class SQLEpisodes:
    """Episode rows and call logs over a backend `Connection`."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def transaction(self) -> AbstractContextManager[None]:
        return self._c.transaction()

    def insert_episode(self, row: EpisodeRow) -> None:
        with self._c.transaction():
            self._c.execute(
                "INSERT INTO episodes (tenant_id, episode_id, kind, seed, case_type, nonce, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    row.tenant_id,
                    row.episode_id,
                    row.kind,
                    row.seed,
                    row.case_type,
                    row.nonce,
                    row.created_at.isoformat(),
                ),
            )

    def get_episode(self, tenant_id: str, episode_id: str) -> EpisodeRow | None:
        rows = self._c.query(
            "SELECT tenant_id, episode_id, kind, seed, case_type, nonce, created_at "
            "FROM episodes WHERE tenant_id=? AND episode_id=?",
            (tenant_id, episode_id),
        )
        return EpisodeRow.model_validate(dict(rows[0])) if rows else None

    def count_starts(self, tenant_id: str, kind: str, seed: int, case_type: str | None) -> int:
        rows = self._c.query(
            "SELECT COUNT(*) AS n FROM episodes WHERE tenant_id=? AND kind=? AND seed=? "
            "AND case_type IS NOT DISTINCT FROM ?",
            (tenant_id, kind, seed, case_type),
        )
        return int(rows[0]["n"])

    def append_call(
        self, tenant_id: str, episode_id: str, seq: int, tool: str, args_json: str, at: datetime
    ) -> None:
        with self._c.transaction():
            self._c.execute(
                "INSERT INTO episode_calls (tenant_id, episode_id, seq, tool, args_json, at) "
                "VALUES (?,?,?,?,?,?)",
                (tenant_id, episode_id, seq, tool, args_json, at.isoformat()),
            )

    def calls(self, tenant_id: str, episode_id: str) -> list[dict[str, Any]]:
        rows = self._c.query(
            "SELECT seq, tool, args_json FROM episode_calls WHERE tenant_id=? AND episode_id=? "
            "ORDER BY seq",
            (tenant_id, episode_id),
        )
        return [dict(r) for r in rows]

    def count_episodes(self, tenant_id: str) -> int:
        rows = self._c.query("SELECT COUNT(*) AS n FROM episodes WHERE tenant_id=?", (tenant_id,))
        return int(rows[0]["n"])


__all__ = ["POSTGRES_DDL", "SQLITE_DDL", "EpisodeRow", "SQLEpisodes"]
