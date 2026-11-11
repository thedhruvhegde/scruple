"""Tables for outbound integrations (`scruple.integrations`), one tenant per row.

    integration_settings   append-only versions of IntegrationSettings (latest wins)
    integration_events     append-only: every gate event sent out (the payload sent)
    integration_cursor     the last audit seq the dispatcher has turned into events
    webhook_endpoints      endpoints (mutable: active flag, sealed secrets)
    webhook_deliveries     one row per (endpoint, event): status, attempts, last error
    webhook_attempts       append-only log of every attempt, replays included
    ticket_links           one issue per candidate per provider (claimed, then filled)
    ticket_updates         append-only: one update per decision per provider
    link_nonces            append-only: used approval-link nonces and Slack signatures

On SQLite they are created when a tenant first configures something
(`scruple.storage.lazy`); on PostgreSQL they are part of the schema (migration 0014).
"""

from __future__ import annotations

from scruple.storage.lazy import Dialect, append_only_triggers, postgres_ddl

_CREATE = (
    """CREATE TABLE IF NOT EXISTS integration_settings (
    tenant_id TEXT NOT NULL, number BIGINT NOT NULL, body_json TEXT NOT NULL,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, number))""",
    """CREATE TABLE IF NOT EXISTS integration_events (
    tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, audit_seq BIGINT NOT NULL,
    event_type TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id))""",
    """CREATE TABLE IF NOT EXISTS integration_cursor (
    tenant_id TEXT NOT NULL PRIMARY KEY, last_seq BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS webhook_endpoints (
    tenant_id TEXT NOT NULL, endpoint_id TEXT NOT NULL, url TEXT NOT NULL,
    events_json TEXT NOT NULL, secrets_json TEXT NOT NULL, active INTEGER NOT NULL,
    description TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, endpoint_id))""",
    """CREATE TABLE IF NOT EXISTS webhook_deliveries (
    tenant_id TEXT NOT NULL, delivery_id TEXT NOT NULL, endpoint_id TEXT NOT NULL,
    event_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
    last_status_code INTEGER, last_error TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, delivered_at TEXT, replays INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, delivery_id))""",
    """CREATE TABLE IF NOT EXISTS webhook_attempts (
    tenant_id TEXT NOT NULL, delivery_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
    at TEXT NOT NULL, status_code INTEGER, error TEXT, duration_ms INTEGER NOT NULL,
    job_id TEXT NOT NULL, PRIMARY KEY (tenant_id, delivery_id, attempt_no))""",
    """CREATE TABLE IF NOT EXISTS ticket_links (
    tenant_id TEXT NOT NULL, provider TEXT NOT NULL, candidate TEXT NOT NULL,
    issue_id TEXT, issue_key TEXT, url TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider, candidate))""",
    """CREATE TABLE IF NOT EXISTS ticket_updates (
    tenant_id TEXT NOT NULL, provider TEXT NOT NULL, decision_id TEXT NOT NULL,
    candidate TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (tenant_id, provider, decision_id))""",
    """CREATE TABLE IF NOT EXISTS link_nonces (
    tenant_id TEXT NOT NULL, nonce TEXT NOT NULL, purpose TEXT NOT NULL,
    used_at TEXT NOT NULL, PRIMARY KEY (tenant_id, nonce))""",
)
TABLES = (
    "integration_settings",
    "integration_events",
    "integration_cursor",
    "webhook_endpoints",
    "webhook_deliveries",
    "webhook_attempts",
    "ticket_links",
    "ticket_updates",
    "link_nonces",
)
APPEND_ONLY = (
    "integration_settings",
    "integration_events",
    "webhook_attempts",
    "ticket_updates",
    "link_nonces",
)


def statements(dialect: Dialect) -> tuple[str, ...]:
    triggers = (sql for t in APPEND_ONLY for sql in append_only_triggers(t, dialect))
    return (*_CREATE, *triggers)


SQLITE_DDL = statements("sqlite")
# Keep in step with migration 0014 (a test compares the catalogs).
POSTGRES_DDL = postgres_ddl(statements("postgres"))

__all__ = ["APPEND_ONLY", "POSTGRES_DDL", "SQLITE_DDL", "TABLES", "statements"]
