"""SQLite-specific behaviour: files, memory, locations. The contract covers the rest."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scruple.storage import (
    ImmutableHistoryError,
    SQLiteStorage,
    StorageError,
    open_storage,
)
from scruple.storage.sql import SQLStorage
from tests.helpers import rv
from tests.storage.test_contract import SPEC, _audit


def test_file_database_persists_across_reopen(tmp_path: Path) -> None:
    path = str(tmp_path / "p.db")
    s = SQLiteStorage(path)
    s.insert_rule_version(rv("r-file", SPEC))
    s.append_audit(_audit(1))
    s.insert_decision("dec-00001", "r-file", 1, "{}")
    s.close()
    s = SQLiteStorage(path)  # schema creation is idempotent
    assert s.get_rule_version("r-file", 1) == rv("r-file", SPEC)
    assert s.list_audit() == [_audit(1)] and s.count_decisions() == 1
    s.close()


def test_memory_databases_are_isolated() -> None:
    a, b = SQLiteStorage(":memory:"), SQLiteStorage(":memory:")
    a.insert_decision("dec-00001", "r-a", 1, "{}")
    assert b.count_decisions() == 0


def test_two_connections_to_one_file_see_each_others_commits(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    a, b = SQLiteStorage(path), SQLiteStorage(path)
    a.insert_decision("dec-00001", "r-a", 1, "{}")
    assert b.get_decision("dec-00001") == "{}"
    a.close()
    b.close()


@pytest.mark.parametrize(
    ("location", "expect_file"),
    [(":memory:", None), ("sqlite:///", None), ("g.db", "g.db"), ("sqlite:///h.db", "h.db")],
)
def test_open_storage_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, expect_file: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    s = open_storage(location)
    assert isinstance(s, SQLiteStorage) and isinstance(s, SQLStorage)
    s.insert_decision("dec-00001", "r-a", 1, "{}")
    s.close()
    if expect_file is not None:
        assert (tmp_path / expect_file).exists()
    else:
        assert list(tmp_path.iterdir()) == []


def test_open_storage_rejects_unknown_schemes() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        open_storage("mysql://db/x")


def test_driver_errors_are_translated() -> None:
    s = SQLiteStorage(":memory:")
    s.insert_rule_version(rv("r-x", SPEC))
    with pytest.raises(ImmutableHistoryError) as info:
        s.unsafe_execute("DELETE FROM rule_versions")
    assert isinstance(info.value, StorageError)
    with pytest.raises(StorageError, match="no such table"):
        s.unsafe_execute("SELECT * FROM nope")


def test_storage_package_imports_first_in_a_fresh_interpreter() -> None:
    # registry -> storage -> registry is a cycle; importing storage first must work.
    out = subprocess.run(
        [sys.executable, "-c", "import scruple.storage; print('ok')"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
