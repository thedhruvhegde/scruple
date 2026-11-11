"""Tables that exist only once a feature is used (policies, integrations).

The gate's own schema (rules, audit, decisions, jobs, ...) is created at open and
by alembic. Features that most deployments never turn on create their tables on
first write instead, with `CREATE TABLE IF NOT EXISTS`, so a database that never
used them is unchanged. Reads check `table_exists` first and treat a missing table
as empty, so reading never creates anything.

The DDL is plain enough for both backends: TEXT, BIGINT, composite primary keys,
no autoincrement (callers allocate keys inside a transaction).

First-use creation is for SQLite only. On PostgreSQL these tables are part of the
schema (`PostgresStorage` and the alembic migrations), so the service can run as a
role that cannot create tables; `ensure_tables` is a no-op there. Append-only tables
get the same refuse-update/delete triggers as the rest of the history
(`append_only_triggers`).
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


def append_only_triggers(table: str, dialect: Dialect) -> tuple[str, ...]:
    """Triggers that refuse UPDATE and DELETE (and TRUNCATE on PostgreSQL) on `table`."""
    msg = f"'{table} is append-only'"
    if dialect == "sqlite":
        return tuple(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table}\n"
            f"BEGIN SELECT RAISE(ABORT, {msg}); END"
            for op in ("UPDATE", "DELETE")
        )
    return (
        *(
            f"CREATE OR REPLACE TRIGGER {table}_no_{op.lower()} BEFORE {op} ON {table}\n"
            f"    FOR EACH ROW EXECUTE FUNCTION scruple_refuse({msg})"
            for op in ("UPDATE", "DELETE")
        ),
        f"CREATE OR REPLACE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}\n"
        f"    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse({msg})",
    )


def postgres_ddl(statements: Iterable[str]) -> str:
    """One script from single statements (for the PostgreSQL schema and migrations)."""
    return "".join(f"{sql};\n" for sql in statements)


def ensure_tables(conn: Connection, statements: Iterable[str], dialect: Dialect) -> None:
    """Run each `CREATE ... IF NOT EXISTS` statement (one statement per string).

    SQLite only: on PostgreSQL the tables already exist (schema and migrations).
    """
    if dialect != "sqlite":
        return
    with conn.transaction():
        for sql in statements:
            conn.execute(sql)


__all__ = [
    "Dialect",
    "append_only_triggers",
    "connection_of",
    "ensure_tables",
    "postgres_ddl",
    "table_exists",
]
