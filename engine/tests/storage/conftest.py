"""Backends under the storage contract. Every test in this package runs on each."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

from scruple.storage import SQLiteStorage, Storage


class RawStorage(Storage, Protocol):
    """A backend plus its raw-SQL escape hatch (tests and the tamper demo only)."""

    def unsafe_execute(self, sql: str) -> None: ...


BACKENDS = [
    "sqlite-memory",
    "sqlite-file",
    pytest.param("postgres", marks=pytest.mark.postgres),
]


@pytest.fixture
def pg_store(postgres_url: str) -> Iterator[RawStorage]:
    """A PostgresStorage in its own fresh schema, dropped afterwards."""
    import psycopg

    from scruple.storage.postgres import PostgresStorage

    schema = f"t_{uuid.uuid4().hex[:16]}"
    s = PostgresStorage(postgres_url, schema=schema)
    yield s
    s.close()
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(params=BACKENDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RawStorage]:
    if request.param == "postgres":
        pg: RawStorage = request.getfixturevalue("pg_store")
        yield pg
        return
    s: RawStorage
    if request.param == "sqlite-memory":
        s = SQLiteStorage(":memory:")
    else:
        s = SQLiteStorage(str(tmp_path / "contract.db"))
    yield s
    s.close()
