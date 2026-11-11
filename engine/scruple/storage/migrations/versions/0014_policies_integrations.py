"""policies and integrations: tenant policy files, webhooks, tickets, approvals

Revision ID: 0014
Revises: 0013

Frozen copy of `scruple.storage.policies.POSTGRES_DDL` and
`scruple.storage.integrations.POSTGRES_DDL` at the time of writing. On SQLite these
tables are created on first use instead. Never edit a released migration; add a new
revision.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_id TEXT NOT NULL,
    number BIGINT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    body_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, number)
);
CREATE OR REPLACE TRIGGER tenant_policies_no_update BEFORE UPDATE ON tenant_policies
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('tenant_policies is append-only');
CREATE OR REPLACE TRIGGER tenant_policies_no_delete BEFORE DELETE ON tenant_policies
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('tenant_policies is append-only');
CREATE OR REPLACE TRIGGER tenant_policies_no_truncate BEFORE TRUNCATE ON tenant_policies
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('tenant_policies is append-only');

CREATE TABLE IF NOT EXISTS integration_settings (
    tenant_id TEXT NOT NULL, number BIGINT NOT NULL, body_json TEXT NOT NULL,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, number));
CREATE TABLE IF NOT EXISTS integration_events (
    tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, audit_seq BIGINT NOT NULL,
    event_type TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id));
CREATE TABLE IF NOT EXISTS integration_cursor (
    tenant_id TEXT NOT NULL PRIMARY KEY, last_seq BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS webhook_endpoints (
    tenant_id TEXT NOT NULL, endpoint_id TEXT NOT NULL, url TEXT NOT NULL,
    events_json TEXT NOT NULL, secrets_json TEXT NOT NULL, active INTEGER NOT NULL,
    description TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, endpoint_id));
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    tenant_id TEXT NOT NULL, delivery_id TEXT NOT NULL, endpoint_id TEXT NOT NULL,
    event_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
    last_status_code INTEGER, last_error TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, delivered_at TEXT, replays INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, delivery_id));
CREATE TABLE IF NOT EXISTS webhook_attempts (
    tenant_id TEXT NOT NULL, delivery_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
    at TEXT NOT NULL, status_code INTEGER, error TEXT, duration_ms INTEGER NOT NULL,
    job_id TEXT NOT NULL, PRIMARY KEY (tenant_id, delivery_id, attempt_no));
CREATE TABLE IF NOT EXISTS ticket_links (
    tenant_id TEXT NOT NULL, provider TEXT NOT NULL, candidate TEXT NOT NULL,
    issue_id TEXT, issue_key TEXT, url TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider, candidate));
CREATE TABLE IF NOT EXISTS ticket_updates (
    tenant_id TEXT NOT NULL, provider TEXT NOT NULL, decision_id TEXT NOT NULL,
    candidate TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (tenant_id, provider, decision_id));
CREATE TABLE IF NOT EXISTS link_nonces (
    tenant_id TEXT NOT NULL, nonce TEXT NOT NULL, purpose TEXT NOT NULL,
    used_at TEXT NOT NULL, PRIMARY KEY (tenant_id, nonce));
CREATE OR REPLACE TRIGGER integration_settings_no_update BEFORE UPDATE ON integration_settings
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('integration_settings is append-only');
CREATE OR REPLACE TRIGGER integration_settings_no_delete BEFORE DELETE ON integration_settings
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('integration_settings is append-only');
CREATE OR REPLACE TRIGGER integration_settings_no_truncate BEFORE TRUNCATE ON integration_settings
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('integration_settings is append-only');
CREATE OR REPLACE TRIGGER integration_events_no_update BEFORE UPDATE ON integration_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('integration_events is append-only');
CREATE OR REPLACE TRIGGER integration_events_no_delete BEFORE DELETE ON integration_events
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('integration_events is append-only');
CREATE OR REPLACE TRIGGER integration_events_no_truncate BEFORE TRUNCATE ON integration_events
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('integration_events is append-only');
CREATE OR REPLACE TRIGGER webhook_attempts_no_update BEFORE UPDATE ON webhook_attempts
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('webhook_attempts is append-only');
CREATE OR REPLACE TRIGGER webhook_attempts_no_delete BEFORE DELETE ON webhook_attempts
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('webhook_attempts is append-only');
CREATE OR REPLACE TRIGGER webhook_attempts_no_truncate BEFORE TRUNCATE ON webhook_attempts
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('webhook_attempts is append-only');
CREATE OR REPLACE TRIGGER ticket_updates_no_update BEFORE UPDATE ON ticket_updates
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('ticket_updates is append-only');
CREATE OR REPLACE TRIGGER ticket_updates_no_delete BEFORE DELETE ON ticket_updates
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('ticket_updates is append-only');
CREATE OR REPLACE TRIGGER ticket_updates_no_truncate BEFORE TRUNCATE ON ticket_updates
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('ticket_updates is append-only');
CREATE OR REPLACE TRIGGER link_nonces_no_update BEFORE UPDATE ON link_nonces
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('link_nonces is append-only');
CREATE OR REPLACE TRIGGER link_nonces_no_delete BEFORE DELETE ON link_nonces
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('link_nonces is append-only');
CREATE OR REPLACE TRIGGER link_nonces_no_truncate BEFORE TRUNCATE ON link_nonces
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('link_nonces is append-only');
"""

DOWNGRADE = """
DROP TABLE IF EXISTS link_nonces;
DROP TABLE IF EXISTS ticket_updates;
DROP TABLE IF EXISTS ticket_links;
DROP TABLE IF EXISTS webhook_attempts;
DROP TABLE IF EXISTS webhook_deliveries;
DROP TABLE IF EXISTS webhook_endpoints;
DROP TABLE IF EXISTS integration_cursor;
DROP TABLE IF EXISTS integration_events;
DROP TABLE IF EXISTS integration_settings;
DROP TABLE IF EXISTS tenant_policies;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
