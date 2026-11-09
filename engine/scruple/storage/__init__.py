"""Persistence for rules, audit entries and decisions.

`Storage` is the facade every caller depends on; it is composed of one repository
protocol per aggregate (see `scruple.storage.base`). `open_storage` picks a backend
from a location string.
"""

from __future__ import annotations

# base must be imported first: registry imports `Storage` from this package while
# sqlite (below) is still importing the registry models.
from scruple.storage.base import (
    AuditRepository,
    DecisionRepository,
    DuplicateKeyError,
    ImmutableHistoryError,
    RuleVersionRepository,
    Storage,
    StorageError,
    Transactional,
)
from scruple.storage.sqlite import SCHEMA, SQLiteStorage

SQLITE_PREFIX = "sqlite:///"
POSTGRES_PREFIXES = ("postgresql://", "postgres://")


def open_storage(location: str) -> Storage:
    """Open a backend from a location.

    - `":memory:"`: SQLite in memory (tests)
    - `"sqlite:///path/to.db"` or a bare path: SQLite file
    - `"postgresql://..."` or `"postgres://..."`: PostgreSQL (schema created if missing)
    """
    if location.startswith(POSTGRES_PREFIXES):
        from scruple.storage.postgres import PostgresStorage  # psycopg loads only if used

        return PostgresStorage(location)
    if location.startswith(SQLITE_PREFIX):
        location = location[len(SQLITE_PREFIX) :] or ":memory:"
    if "://" in location:
        raise ValueError(f"unsupported storage location {location.split('://')[0]}://")
    return SQLiteStorage(location)


__all__ = [
    "SCHEMA",
    "AuditRepository",
    "DecisionRepository",
    "DuplicateKeyError",
    "ImmutableHistoryError",
    "RuleVersionRepository",
    "SQLiteStorage",
    "Storage",
    "StorageError",
    "Transactional",
    "open_storage",
]
