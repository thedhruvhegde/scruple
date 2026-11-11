"""The episode tables accept every kind the stores serve, old SQLite files included."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scruple.agents.store import KINDS
from scruple.storage import SQLiteStorage
from scruple.storage.episodes import EPISODE_KINDS, KIND_CHECK

OLD_EPISODES = """
CREATE TABLE episodes (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    episode_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('simple', 'dispute', 'scenario', 'ach', 'recon')),
    seed BIGINT NOT NULL,
    case_type TEXT,
    nonce INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, episode_id)
);
INSERT INTO episodes VALUES ('default', 'ep-000000000001', 'ach', 3, NULL, 0, '2026-11-01');
"""


def _insert(path: Path, kind: str, n: int) -> None:
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO episodes VALUES ('default', ?, ?, 1, NULL, 0, '2026-11-02')",
            (f"ep-{n:012d}", kind),
        )


def test_every_store_kind_is_allowed() -> None:
    assert set(KINDS) <= set(EPISODE_KINDS)
    assert all(f"'{k}'" in KIND_CHECK for k in EPISODE_KINDS)


def test_an_old_sqlite_file_is_rebuilt_with_the_new_kinds(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.executescript(OLD_EPISODES)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(path, "aml", 2)
    SQLiteStorage(str(path)).close()
    _insert(path, "aml", 2)
    _insert(path, "payables", 3)
    with sqlite3.connect(path) as c:
        rows = c.execute("SELECT episode_id, kind FROM episodes ORDER BY episode_id").fetchall()
        assert rows[0] == ("ep-000000000001", "ach")  # the old row survives unchanged
        assert [r[1] for r in rows] == ["ach", "aml", "payables"]
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("UPDATE episodes SET seed = 9")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO episodes VALUES ('default', ?, ?, 1, NULL, 0, 'x')",
                ("ep-000000000009", "nope"),
            )
    SQLiteStorage(str(path)).close()  # a second open changes nothing
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM episodes").fetchone() == (3,)
