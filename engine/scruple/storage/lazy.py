"""Tables that exist only once a feature is used (policies, integrations).

The gate's own schema (rules, audit, decisions, jobs, ...) is created at open and
by alembic. Features that most deployments never turn on create their tables on
first write instead, with `CREATE TABLE IF NOT EXISTS`, so a database that never
used them is unchanged. Reads check `table_exists` first and treat a missing table
as empty, so reading never creates anything.

The DDL is plain enough for both backends: TEXT, BIGINT, composite primary keys,
no autoincrement (callers allocate keys inside a transaction).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from scruple.storage.sql import Connection

Dialect = Literal["sqlite", "postgres"]


def connection_of(storage: object) -> tuple[Connection, Dialect]:
    """The backend connection of a gate `Storage` and its dialect."""
    from scruple.storage.postgres import PostgresStorage
    from scruple.storage.sqlite import SQLiteStorage

    if isinstance(storage, PostgresStorage):
        return storage.connection, "postgres"
    if isinstance(storage, SQLiteStorage):
        return storage.connection, "sqlite"
    raise TypeError(f"{type(storage).__name__} has no SQL connection")


def table_exists(conn: Connection, dialect: Dialect, name: str) -> bool:
    if dialect == "sqlite":
        rows = conn.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    else:
        rows = conn.query(
            "SELECT 1 FROM pg_tables WHERE tablename=? AND schemaname=current_schema()", (name,)
        )
    return bool(rows)


def ensure_tables(conn: Connection, statements: Iterable[str]) -> None:
    """Run each `CREATE ... IF NOT EXISTS` statement (one statement per string)."""
    with conn.transaction():
        for sql in statements:
            conn.execute(sql)


__all__ = ["Dialect", "connection_of", "ensure_tables", "table_exists"]
