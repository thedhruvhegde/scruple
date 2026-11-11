"""Saved console filters (`scruple.console`), per tenant, as an append-only log.

A saved filter is a name for a canonical query string on one console list (rule
versions, gate runs, cases, approvals). Saving appends a `save` row; saving the same
name again appends another (the latest wins); deleting appends a `delete` row. Nothing
is updated or removed, so who saved or deleted what, and when, stays on record. The
filter id is derived from (list, name), so a name means one filter per list.

Every read and write names its tenant; another tenant's filters are never visible.
The query string is stored exactly as `scruple.console.listing` canonicalised it
(known keys only, values already validated) and is rendered escaped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from scruple.storage.sql import Connection

Lists = Literal["versions", "runs", "cases", "approvals"]
LISTS: tuple[Lists, ...] = ("versions", "runs", "cases", "approvals")
MAX_PER_LIST = 50

_COLS = """
    tenant_id TEXT NOT NULL DEFAULT 'default',
    filter_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('save', 'delete')),
    list TEXT NOT NULL CHECK (list IN ('versions', 'runs', 'cases', 'approvals')),
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL
"""
_T = "console_filter_events"
_INDEX = f"CREATE INDEX IF NOT EXISTS {_T}_tenant\n    ON {_T} (tenant_id, list, filter_id);\n"

SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS {_T} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,{_COLS});
{_INDEX}CREATE TRIGGER IF NOT EXISTS {_T}_no_update BEFORE UPDATE ON {_T}
BEGIN SELECT RAISE(ABORT, '{_T} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {_T}_no_delete BEFORE DELETE ON {_T}
BEGIN SELECT RAISE(ABORT, '{_T} is append-only'); END;
"""

# Keep in step with migration 0011 (a test compares the catalogs).
POSTGRES_DDL = f"""
CREATE TABLE IF NOT EXISTS {_T} (
    id BIGSERIAL PRIMARY KEY,{_COLS});
{_INDEX}CREATE OR REPLACE TRIGGER {_T}_no_update BEFORE UPDATE ON {_T}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{_T} is append-only');
CREATE OR REPLACE TRIGGER {_T}_no_delete BEFORE DELETE ON {_T}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{_T} is append-only');
CREATE OR REPLACE TRIGGER {_T}_no_truncate BEFORE TRUNCATE ON {_T}
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('{_T} is append-only');
"""


class SavedFilterError(ValueError):
    """Refused: unknown list, too many filters, not yours to delete."""


@dataclass(frozen=True)
class SavedFilter:
    filter_id: str
    list: str
    name: str
    query: str
    saved_by: str
    saved_at: datetime


def filter_id(list_: str, name: str) -> str:
    digest = hashlib.sha256(f"{list_}\x00{name}".encode()).hexdigest()
    return f"flt-{digest[:12]}"


class SQLSavedFilters:
    """Saved filters over a backend `Connection` (the gate database's)."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def _append(
        self,
        tenant: str,
        fid: str,
        action: str,
        list_: str,
        name: str,
        query: str,
        actor: str,
        at: datetime,
    ) -> None:
        self._c.execute(
            f"INSERT INTO {_T} (tenant_id, filter_id, action, list, name, query, actor, at) "  # noqa: S608
            "VALUES (?,?,?,?,?,?,?,?)",
            (tenant, fid, action, list_, name, query, actor, at.isoformat()),
        )

    def list(self, tenant: str, list_: str) -> list[SavedFilter]:
        """The tenant's live filters on one list, by name."""
        rows = self._c.query(
            f"SELECT filter_id, action, list, name, query, actor, at FROM {_T} "  # noqa: S608
            "WHERE tenant_id=? AND list=? ORDER BY id",
            (tenant, list_),
        )
        live: dict[str, SavedFilter] = {}
        for r in rows:
            if r["action"] == "delete":
                live.pop(r["filter_id"], None)
            else:
                live[r["filter_id"]] = SavedFilter(
                    r["filter_id"],
                    r["list"],
                    r["name"],
                    r["query"],
                    r["actor"],
                    datetime.fromisoformat(r["at"]),
                )
        return sorted(live.values(), key=lambda f: f.name)

    def get(self, tenant: str, fid: str) -> SavedFilter | None:
        for list_ in LISTS:
            for f in self.list(tenant, list_):
                if f.filter_id == fid:
                    return f
        return None

    def save(
        self, tenant: str, list_: str, name: str, query: str, actor: str, at: datetime
    ) -> SavedFilter:
        if list_ not in LISTS:
            raise SavedFilterError(f"unknown list {list_!r}")
        fid = filter_id(list_, name)
        with self._c.transaction():
            current = self.list(tenant, list_)
            if len(current) >= MAX_PER_LIST and all(f.filter_id != fid for f in current):
                raise SavedFilterError(f"at most {MAX_PER_LIST} saved filters per list")
            self._append(tenant, fid, "save", list_, name, query, actor, at)
        return SavedFilter(fid, list_, name, query, actor, at)

    def delete(self, tenant: str, fid: str, actor: str, at: datetime) -> SavedFilter:
        """Delete a filter. Only whoever saved it last may delete it."""
        with self._c.transaction():
            f = self.get(tenant, fid)
            if f is None:
                raise LookupError(f"no saved filter {fid}")
            if f.saved_by != actor:
                raise SavedFilterError(f"only {f.saved_by} may delete this filter")
            self._append(tenant, fid, "delete", f.list, f.name, f.query, actor, at)
        return f


def saved_filters_for(storage: object) -> SQLSavedFilters | None:
    """The saved-filter store on a gate `Storage`'s database, or None if it has none."""
    from scruple.storage.sql import SQLStorage

    if isinstance(storage, SQLStorage):
        return SQLSavedFilters(storage.connection)
    return None


__all__ = [
    "LISTS",
    "MAX_PER_LIST",
    "POSTGRES_DDL",
    "SQLITE_DDL",
    "SQLSavedFilters",
    "SavedFilter",
    "SavedFilterError",
    "filter_id",
    "saved_filters_for",
]
