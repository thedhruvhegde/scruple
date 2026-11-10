"""api keys: hashed secrets, scopes, revocation, last use (scruple.auth)

Revision ID: 0002
Revises: 0001

Frozen copy of the api_keys DDL in scruple/storage/postgres.py at the time of
writing. Never edit a released migration; add a new revision.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    roles_json TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    description TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT,
    rate_per_minute INTEGER,
    burst INTEGER,
    max_body_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS api_keys_tenant ON api_keys (tenant_id, created_at);

CREATE OR REPLACE FUNCTION scruple_api_key_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.key_id IS DISTINCT FROM OLD.key_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.roles_json IS DISTINCT FROM OLD.roles_json
       OR NEW.scopes_json IS DISTINCT FROM OLD.scopes_json
       OR NEW.secret_hash IS DISTINCT FROM OLD.secret_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.description IS DISTINCT FROM OLD.description
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.rate_per_minute IS DISTINCT FROM OLD.rate_per_minute
       OR NEW.burst IS DISTINCT FROM OLD.burst
       OR NEW.max_body_bytes IS DISTINCT FROM OLD.max_body_bytes
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'api key is immutable except last use and one revocation';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE TRIGGER api_keys_immutable BEFORE UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION scruple_api_key_immutable();
CREATE OR REPLACE TRIGGER api_keys_no_delete BEFORE DELETE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('api keys are append-only; revoke instead');
CREATE OR REPLACE TRIGGER api_keys_no_truncate BEFORE TRUNCATE ON api_keys
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('api keys are append-only; revoke instead');
"""

DOWNGRADE = """
DROP TABLE IF EXISTS api_keys;
DROP FUNCTION IF EXISTS scruple_api_key_immutable();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
