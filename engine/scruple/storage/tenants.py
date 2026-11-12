"""Table for tenant administration (`scruple.tenancy`): what was done to each tenant.

`tenant_events` is append-only: one row per administrative change to a tenant
(`created`, `quota_set`, `offboarded`), numbered from 1 per tenant. A tenant's state
is the fold of its rows. On SQLite it is created on the first change
(`scruple.storage.lazy`); on PostgreSQL it is part of the schema (migration 0015).
"""

from __future__ import annotations

from scruple.storage.lazy import Dialect, append_only_triggers, postgres_ddl

TABLE = "tenant_events"
_CREATE = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    tenant_id TEXT NOT NULL,
    number BIGINT NOT NULL,
    kind TEXT NOT NULL,
    body_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, number)
)"""


def statements(dialect: Dialect) -> tuple[str, ...]:
    return (_CREATE, *append_only_triggers(TABLE, dialect))


SQLITE_DDL = statements("sqlite")
# Keep in step with migration 0015 (a test compares the catalogs).
POSTGRES_DDL = postgres_ddl(statements("postgres"))

__all__ = ["POSTGRES_DDL", "SQLITE_DDL", "TABLE", "statements"]
