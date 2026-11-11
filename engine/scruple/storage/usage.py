"""Tables for usage metering (`scruple.usage`): append-only usage events per tenant.

One row per metered event: which meter, how much, when it happened (`at`, the
decision's own time), what it came from (`source`, e.g. a decision id) and when it
was recorded. `event_key` is unique per tenant, so recording the same event twice is
a no-op (`ON CONFLICT DO NOTHING`), which makes metering idempotent and safe to
re-run. Nothing is updated or deleted; a correction is a new event with its own key.

Timestamps are ISO 8601 UTC text with microseconds, so they order lexicographically
on both backends.
"""

from __future__ import annotations

_T = "usage_events"
_COLS = """
    tenant_id TEXT NOT NULL DEFAULT 'default',
    event_key TEXT NOT NULL,
    meter TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity >= 0),
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    rule_id TEXT,
    version INTEGER,
    recorded_at TEXT NOT NULL
"""
_INDEXES = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_T}_key ON {_T} (tenant_id, event_key);
CREATE INDEX IF NOT EXISTS {_T}_at ON {_T} (tenant_id, at);
"""

SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS {_T} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,{_COLS});
{_INDEXES}
CREATE TRIGGER IF NOT EXISTS {_T}_no_update BEFORE UPDATE ON {_T}
BEGIN SELECT RAISE(ABORT, '{_T} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {_T}_no_delete BEFORE DELETE ON {_T}
BEGIN SELECT RAISE(ABORT, '{_T} is append-only'); END;
"""

# Keep in step with migration 0012 (a test compares the catalogs).
POSTGRES_DDL = f"""
CREATE TABLE IF NOT EXISTS {_T} (
    id BIGSERIAL PRIMARY KEY,{_COLS});
{_INDEXES}
CREATE OR REPLACE TRIGGER {_T}_no_update BEFORE UPDATE ON {_T}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{_T} is append-only');
CREATE OR REPLACE TRIGGER {_T}_no_delete BEFORE DELETE ON {_T}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{_T} is append-only');
CREATE OR REPLACE TRIGGER {_T}_no_truncate BEFORE TRUNCATE ON {_T}
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('{_T} is append-only');
"""

__all__ = ["POSTGRES_DDL", "SQLITE_DDL"]
