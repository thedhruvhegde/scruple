"""tenancy: tenant_id on every history row, primary keys per tenant

Revision ID: 0003
Revises: 0002

Existing rows join the tenant "default"; their contents, and so every audit hash,
are unchanged. Each tenant then has its own rule versions, decision numbering and
audit hash chain. Adding a column with a default fires no row triggers, so the
append-only guards stay in place throughout. Frozen copy of the tenancy DDL in
scruple/storage/postgres.py at the time of writing.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE = """
-- tenancy (migration 0003): every history row belongs to a tenant
ALTER TABLE rule_versions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'rule_versions'::regclass AND contype = 'p'
          AND pg_get_constraintdef(oid) LIKE '%tenant_id%'
    ) THEN
        ALTER TABLE rule_versions DROP CONSTRAINT rule_versions_pkey,
            ADD CONSTRAINT rule_versions_pkey PRIMARY KEY (tenant_id, rule_id, version);
        ALTER TABLE audit_events DROP CONSTRAINT audit_events_pkey,
            ADD CONSTRAINT audit_events_pkey PRIMARY KEY (tenant_id, seq);
        ALTER TABLE decisions DROP CONSTRAINT decisions_pkey,
            ADD CONSTRAINT decisions_pkey PRIMARY KEY (tenant_id, decision_id);
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION scruple_rule_content_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rule_id IS DISTINCT FROM OLD.rule_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.spec_json IS DISTINCT FROM OLD.spec_json
       OR NEW.owner IS DISTINCT FROM OLD.owner
       OR NEW.provenance_json IS DISTINCT FROM OLD.provenance_json
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION 'rule content is immutable';
    END IF;
    RETURN NEW;
END
$$;
"""

# Down only while every row is in the default tenant: the old keys cannot hold two
# tenants' rows, so the downgrade fails (and changes nothing) if another exists.
DOWNGRADE = """
ALTER TABLE rule_versions DROP CONSTRAINT rule_versions_pkey,
    ADD CONSTRAINT rule_versions_pkey PRIMARY KEY (rule_id, version);
ALTER TABLE audit_events DROP CONSTRAINT audit_events_pkey,
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (seq);
ALTER TABLE decisions DROP CONSTRAINT decisions_pkey,
    ADD CONSTRAINT decisions_pkey PRIMARY KEY (decision_id);
CREATE OR REPLACE FUNCTION scruple_rule_content_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rule_id IS DISTINCT FROM OLD.rule_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.spec_json IS DISTINCT FROM OLD.spec_json
       OR NEW.owner IS DISTINCT FROM OLD.owner
       OR NEW.provenance_json IS DISTINCT FROM OLD.provenance_json
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'rule content is immutable';
    END IF;
    RETURN NEW;
END
$$;
ALTER TABLE rule_versions DROP COLUMN tenant_id;
ALTER TABLE audit_events DROP COLUMN tenant_id;
ALTER TABLE decisions DROP COLUMN tenant_id;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
