"""Backends under the storage contract. Every test in this package runs on each."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

from scruple.storage import SQLiteStorage, Storage


class RawStorage(Storage, Protocol):
    """A backend plus its raw-SQL escape hatch (tests and the tamper demo only)."""

    def unsafe_execute(self, sql: str) -> None: ...


BACKENDS = ["sqlite-memory", "sqlite-file"]


@pytest.fixture(params=BACKENDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RawStorage]:
    s: RawStorage
    if request.param == "sqlite-memory":
        s = SQLiteStorage(":memory:")
    else:
        s = SQLiteStorage(str(tmp_path / "contract.db"))
    yield s
    s.close()
