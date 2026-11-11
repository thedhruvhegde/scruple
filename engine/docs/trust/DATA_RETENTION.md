# Data retention and deletion policy

Owner: the founder. Review: every six months. Not legal advice: how long a partner
must keep dispute records is the partner's obligation and counsel's question; this
file only describes what scruple keeps and how it deletes. See README.md for the
status words.

## What scruple holds

| data | where | kind | default retention |
|---|---|---|---|
| imported dispute cases and ledger movements | case store (SQLite file per tenant, `scruple/ingest/store.py`) | partner data, redacted, card numbers tokenised, chosen fields encrypted | the pilot, then deleted (docs/pilot/DATA_CHECKLIST.md); or a per-tenant policy in days |
| decisions and outcomes production sent to shadow mode | `imported_shadow_events` in the gate database | partner data | as the tenant's retention policy |
| rules, decisions, approvals, audit log | gate database | derived records; no case rows | append-only for the life of the database |
| ingest reports, backtest and shadow reports, scorecards, evidence packs | files the operator writes | aggregates, small cells suppressed (k = 10) | as the pilot terms allow |
| API keys | `api_keys` table | scrypt hashes, never the key | for the life of the database; revoked keys stay as records |
| logs | stderr, one JSON object per line | request metadata; no request bodies | whatever the platform keeps; scruple keeps none itself |
| backups | `deploy/scripts/backup.sh` output, RDS backups in the Terraform skeleton | copies of the gate database | 14 days in the Terraform default (`db_backup_retention_days`); logical dumps as the operator keeps them |

## Controls

| control | how | status |
|---|---|---|
| per-tenant retention in days, audited purges | `scruple retention set --tenant T --days N`, `scruple retention apply` (gate database and, with `--case-store`, the tenant's store); each purge appends `retention.purged` (table, cutoff, row count, SHA-256 over the deleted rows) to the tenant's chain (`scruple/storage/retention.py`) | code |
| scheduled retention | `scruple worker --schedule-retention`, or `scruple retention schedule` from cron: one job per tenant per UTC day, idempotent | code; nothing runs it unless deployed |
| history is not deleted row by row | triggers refuse UPDATE/DELETE on audit events and decisions; retention passes only for row-level case data | code |
| crypto-shredding | removing a field or token key makes its ciphertexts and tokens unrecoverable (`scruple/security/crypto.py`, `pan.py`) | code; key custody is the partner's (process) |
| no free text, no card numbers | evidence kept as field names; redaction; the store refuses a record whose clear text holds a Luhn-valid card number (tests/test_ingest_pipeline.py) | code |
| end-of-pilot deletion and written confirmation | delete the case store, the gate database, reports and keys (docs/pilot/RUNBOOK.md "Deletion") | process |
| deletion from backups | backups expire on their own schedule; a deleted pilot's data stays in backups until they expire | process; say so in the confirmation |
| synthetic and real data never mixed | a case store holds one or the other (docs/INTEGRATIONS.md); results say which | code (store), process (labelling) |

## Not yet

- Retention for log files and evidence-pack files is the operator's; scruple does not
  delete files it wrote.
- No legal-hold mechanism: a hold today means not running `retention apply` for that
  tenant (and saying so in writing).
- No per-tenant encryption keys; keys are supplied by the operator.
