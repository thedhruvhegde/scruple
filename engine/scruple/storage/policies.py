"""Table for tenant policy files (`scruple.policy`): every policy put in force.

`tenant_policies` is append-only: one row per applied policy, numbered from 1 per
tenant; the highest number is current. On SQLite it is created on the first apply
(`scruple.storage.lazy`); on PostgreSQL it is part of the schema (migration 0014).
"""

from __future__ import annotations

from scruple.storage.lazy import Dialect, append_only_triggers, postgres_ddl

TABLE = "tenant_policies"
_CREATE = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    tenant_id TEXT NOT NULL,
    number BIGINT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    body_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, number)
)"""


def statements(dialect: Dialect) -> tuple[str, ...]:
    return (_CREATE, *append_only_triggers(TABLE, dialect))


SQLITE_DDL = statements("sqlite")
# Keep in step with migration 0014 (a test compares the catalogs).
POSTGRES_DDL = postgres_ddl(statements("postgres"))

__all__ = ["POSTGRES_DDL", "SQLITE_DDL", "TABLE", "statements"]
