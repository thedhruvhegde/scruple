"""The billing ledger (`scruple.billing`): every usage total pushed to a provider.

One append-only row per accepted push: tenant, meter, the UTC day it covers, how much
was added (`delta`), the running total for that day after it (`cumulative`), the
provider's event name and customer, the idempotency identifier sent, and the time.
The identifier is derived from (tenant, meter, day, cumulative), so a retry of the
same push sends the same identifier and a later push for the same day (more usage
arrived) sends a new one with only the difference. Unique per tenant; never updated
or deleted.
"""

from __future__ import annotations

_T = "billing_pushes"
_COLS = """
    tenant_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT NOT NULL,
    identifier TEXT NOT NULL,
    meter TEXT NOT NULL,
    period_start TEXT NOT NULL,
    delta BIGINT NOT NULL CHECK (delta > 0),
    cumulative BIGINT NOT NULL CHECK (cumulative > 0),
    event_name TEXT NOT NULL,
    customer TEXT NOT NULL,
    livemode INTEGER NOT NULL,
    pushed_at TEXT NOT NULL
"""
_INDEXES = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_T}_identifier
    ON {_T} (tenant_id, provider, identifier);
CREATE INDEX IF NOT EXISTS {_T}_period
    ON {_T} (tenant_id, provider, meter, period_start);
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
