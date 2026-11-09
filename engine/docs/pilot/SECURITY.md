# Security questionnaire answers (pilot)

Every answer below points at code and tests in this repository. If a control is not in
the code, it is in "Not yet" at the end, not in the answers. Statements are about the
engine as of November 2026; re-check against the code before sending.

Short version: scruple is a single Python service and command line, run by the partner
or by scruple on one machine. It reads dispute exports read-only, keeps no free text,
never reads card numbers, and records every state change in a hash-chained,
append-only audit log. It has no real authentication yet, no SOC 2 and no external
penetration test.

## 1. Deployment and hosting

| question | answer |
|---|---|
| Where does it run? | In the partner's environment by default: a Docker image (`Dockerfile`) or the Python package (`uv sync`, then `scruple ...`). `docker-compose.yml` runs the API with PostgreSQL 16. |
| Customer VPC / on-premises? | Yes, by running the image or package inside it. There is no Helm chart and no Terraform module yet (ROADMAP 188, 189: not built). |
| Hosted SaaS? | No. There is no multi-tenant hosted service. |
| Container hardening | Slim base image, non-root user (uid 10001), no secrets baked in, pinned `uv` image and frozen lockfile (tests/test_deploy.py). Compose runs the API with a read-only root filesystem, `no-new-privileges`, tmpfs `/tmp`, ports bound to 127.0.0.1. |
| Outbound network | None needed for the gate. The Stripe client makes read-only `GET` requests to Stripe only when ingest is pointed at the API; tests make no network calls (HTTP clients take an injected transport). |
| Dependencies | Locked in `uv.lock`; CI installs with `uv sync --locked`. |

## 2. Data handling

| question | answer |
|---|---|
| What data is processed? | Dispute records: ids, amounts, currency, status, reason, network reason code, dates, which evidence fields are present, balance movements. Field list: DATA_CHECKLIST.md. |
| Card numbers (PANs)? | No adapter reads a PAN. A CSV/JSONL column named with `scruple ingest --pan-column` is tokenised before storage, and the case store refuses any record whose clear text still holds a Luhn-valid card number (tests/test_ingest_pipeline.py, property over generated files with card numbers in every column; the store file's bytes are searched). `scruple.security.pan.PanTokenizer` replaces it with a keyed HMAC-SHA256 token (`ctok_...`) plus first six, last four and length. Tokens rotate by key id; dropping a key retires its tokens. Property test: tokenise, log and write to SQLite, then search the bytes for the PAN; it never appears (tests/test_ingest_tokens.py). |
| PANs in logs | `PanScrubbingFilter` rewrites Luhn-valid card numbers in log messages and arguments. It is installed on the `scruple.security.pan` logger; installing it on the root logger is a deployment step. |
| Free text and PII | Not stored: Stripe evidence is recorded as field names only; the Adyen `reason` text is dropped; NACHA account numbers are cut to the last four, receiver names and ids are not kept. `scruple.security.redact` replaces emails, card numbers, IBANs, long numbers, phones, IPs, common address patterns and cued names (tests/test_ingest_redact.py, including ReDoS time bounds and obfuscated formats). Bank statement descriptions are redacted on construction. |
| Redaction limits | Pinned in tests: a bare name with no cue and not supplied in `known_names` is not found; addresses outside the listed patterns are not found; long non-card numbers are over-redacted as `[NUMBER]`. |
| Encryption at rest (field level) | `scruple.security.crypto.FieldCipher`: AES-256-GCM, 32-byte keys with ids, a fresh 96-bit nonce per value, associated data binding version, key id and record/field context, so a ciphertext moved to another field or record fails. Rotation re-encrypts under the active key; removing a key crypto-shreds its values. Tests flip every ciphertext byte, swap contexts, relabel keys and check a pinned vector against an independent decrypt (tests/test_ingest_crypto.py). |
| Encryption at rest (database) | Not provided by scruple; use the partner's disk or database encryption. Field encryption is applied by `scruple ingest` to the columns or paths the operator names (`--encrypt-column`, `--encrypt-path`, key `SCRUPLE_FIELD_KEYS`); the rest of a stored case is redacted clear text. |
| Encryption in transit | Not terminated by scruple; run it behind the partner's TLS proxy or load balancer. |
| Test mode first | Stripe live keys and live objects, and Adyen `live: "true"` notifications, are refused unless the code sets `allow_live=True` explicitly. |
| Data used for training? | No. scruple trains no models on partner data. |
| Retention and deletion | Manual and documented (DATA_CHECKLIST.md). Pilot data is deleted by deleting the pilot database and keys. No retention-policy code yet. |

## 3. Input validation

| question | answer |
|---|---|
| API inputs | Pydantic models with `extra="forbid"`, nested all the way down; unknown query parameters and bodies on bodiless routes return 422 (`scruple/api/strict.py`, tests/test_strict_inputs.py). |
| File and webhook inputs | Byte limits before decoding, JSON depth limits, strict UTF-8, no NaN, no duplicate keys, no `eval`, XML only through defusedxml with DTDs, entities and external references forbidden (billion laughs and XXE tests). Every parser returns a value or raises an `IngestError` subclass; hypothesis fuzz tests check nothing else escapes. Unknown enum values raise instead of being guessed. |
| Webhook authenticity | Stripe: `Stripe-Signature` HMAC-SHA256 with timestamp tolerance, future timestamps rejected, constant-time compare, several secrets for rotation, replayed event ids acknowledged and not processed again. Adyen: HMAC per item checked against Adyen's own vectors; one bad item rejects the delivery. |
| CSV injection | Values are only strings; nothing is evaluated (`=1+1` stays text). |
| Rule language | Rules can reference only a whitelist of observable fields (`scruple.domain.FIELD_TYPES`); ground truth, labels and ids are not addressable. |

## 4. Integrity and audit

| question | answer |
|---|---|
| Audit log | Every state change is an audit entry `{seq, at, event_type, actor, payload, prev_hash}` hashed with SHA-256 over canonical JSON, chained to the previous entry (`scruple/audit/log.py`). |
| Append-only | Enforced in the database: triggers refuse UPDATE and DELETE on audit events and decisions, and content changes and DELETE on rule versions; on Postgres also TRUNCATE. On Postgres the service runs as a role that does not own the tables and cannot drop the triggers (docs/ARCHITECTURE.md section 9). |
| Tamper detection | `scruple audit verify` recomputes the chain and replays it against the registry and stored decisions. Tests: edited payload, deleted entry, reordered entries, a re-hashed edited entry, raw SQLite file edits of the audit log, of a rule status and of a decision (tests/test_audit.py). The demo shows a rewritten BLOCK decision being detected. |
| Truncation | Detected only against an externally anchored head (`--expected-head`). Anchoring is manual today. |
| Evidence export | `scruple ci evidence` writes sorted JSON plus a PDF with the scorecard, statistics, decisions, approvals, config hash, seed commitments and an audit segment whose hashes recompute to the chain head. Seeds are never exported. |
| Reproducibility | Every random source takes an explicit seed; the same candidate and config give the same scorecard bytes, decision hashes and audit head (tested; the demo output is tested to be identical across runs). |

## 5. Access and separation of duties

| question | answer |
|---|---|
| Authentication | **Not implemented.** The HTTP API trusts the `X-Actor` header as the caller's identity (`scruple/api/app.py`, stated in its docstring). Run the API only on a private network, behind the partner's authenticating proxy, for the pilot. |
| Separation of duties | The rule's owner, its proposer and the learning agent can never approve its activation; approvals can require N of M named approvers, optionally one with a required role; grants and requests expire (default 72 hours and 7 days) (`scruple/gate/approval.py`, tests/test_gate_approval.py). Activation always needs a named human approver (docs/DECISIONS.md D-012). |
| Agent tool access | The gate MCP server does not expose approve or rollback (`scruple/mcp/gate_server.py`). |
| Secrets | The database password is accepted only from the environment, never from config files; a password inside a `database_url` read from a file is refused (`scruple/settings.py`, tests/test_settings.py). Pre-commit runs gitleaks and detect-private-key. |

## 6. Software development

| question | answer |
|---|---|
| Testing | About 2,350 tests in the default suite (ruff, ruff format, mypy strict, pytest), plus slow and Postgres suites. CI workflow `.github/workflows/ci.yml` runs lint, typecheck, tests with coverage, the demo, Postgres tests with migrations, a compose check and an image build. |
| Change control | Small commits; every commit passes `make lint typecheck test`. |
| Vulnerability scanning | Not set up (no dependency or image scanner in CI). |

## Not yet

Checked against the code on the date above. Each is either planned in docs/ROADMAP.md or
recorded in docs/DESIGN.md.

| gap | status |
|---|---|
| SOC 2 | Not started. |
| Penetration test | None yet, internal or external. |
| Authentication (OIDC, SAML, mTLS) and RBAC | Not built; `X-Actor` is trusted (ROADMAP 157, 158). |
| Multi-tenancy and tenant isolation | Not built (ROADMAP 159). One deployment per partner. |
| Key management | No KMS integration, no key-encryption keys, no per-tenant keys; keys are supplied by the operator. Nothing counts encryptions per key yet (NIST SP 800-38D limit noted in docs/INTEGRATIONS.md). |
| Field encryption, redaction and tokenisation wired into storage and ingest | Done for imported cases (`scruple ingest`, case store; docs/INTEGRATIONS.md). Not applied to the gate database (rules, decisions, audit), which holds no partner case data. Webhook batches are not persisted. |
| External anchoring or signing of the audit head | Not built (ROADMAP 141; docs/DESIGN.md "Next" item 4). A consistent rewrite of the whole chain is not detected without an external anchor (docs/VERIFIER_FINDINGS.md G-6). |
| Retention policies | Not built (ROADMAP 155). |
| Persisting ingested webhook batches | Not built; accepted Stripe webhook batches stay in memory on the receiver (docs/INTEGRATIONS.md). |
| Adyen HTTP endpoint | Not built; the adapter is a function. |
| Secrets from a vault | Not built (ROADMAP 156); environment variables only. |
| Helm chart, Terraform module | Not built (ROADMAP 188, 189). |
| Backups and restore runbook | Not written (ROADMAP 191). |
| CI on github.com and gitlab.com | The action and template were tested locally and through a fake HTTP transport, not run on those hosts (docs/CI.md). |
| Image build in this review | The image build is a CI job and is covered statically by tests/test_deploy.py; it was not rebuilt for this document. |
