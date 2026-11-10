# Security questionnaire answers (pilot)

Every answer below points at code and tests in this repository. If a control is not in
the code, it is in "Not yet" at the end, not in the answers. Statements are about the
engine as of November 2026; re-check against the code before sending.

Short version: scruple is a single Python service and command line, run by the partner
or by scruple on one machine. It reads dispute exports read-only, keeps no free text,
never reads card numbers, and records every state change in a hash-chained,
append-only audit log. Every API request is authenticated (scrypt-hashed API keys
or OIDC bearer tokens), every route declares the permission it needs, and every
stored row belongs to a tenant. It has no SOC 2, no SAML or mTLS and no external
penetration test.

## 1. Deployment and hosting

| question | answer |
|---|---|
| Where does it run? | In the partner's environment by default: a Docker image (`Dockerfile`) or the Python package (`uv sync`, then `scruple ...`). `docker-compose.yml` runs the API with PostgreSQL 16. |
| Customer VPC / on-premises? | Yes, by running the image or package inside it. There is no Helm chart and no Terraform module yet (ROADMAP 188, 189: not built). |
| Hosted SaaS? | No. The engine is multi-tenant (section 5), but scruple runs no hosted service. |
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
| Authentication | Every HTTP request is authenticated before any route runs (`scruple/auth/middleware.py`). Two credentials: API keys and OIDC bearer tokens. The caller's identity, recorded as the actor in the audit log, comes from the credential; the `X-Actor` header is ignored (tests/test_auth_keys.py). A development-only mode that trusts `X-Actor` exists (`SCRUPLE_AUTH_DEV_ACTOR_HEADER=true`); it is off by default, logs a warning at start and flags every response with `X-Scruple-Insecure-Dev-Auth: true`. |
| API keys | `scr_<16 hex id>_<256-bit secret>`, shown once by `scruple keys create`. Stored as salted scrypt (n=2^14, r=8, p=1; `hashlib.scrypt`), never in clear, never logged (tests search the database file and every log record for the secret). Lookup by the public id, then a constant-time compare; unknown ids, revoked and expired keys cost one scrypt too and all answer `invalid API key`. A verified key skips scrypt for 5 minutes (in memory, keyed by SHA-256 of the key), but its row is read on every request, so `scruple keys revoke` takes effect on the next request. `last_used_at` is written at most once a minute. Key creation and revocation are audit events. Database triggers refuse any change to a key except its last use and one revocation, and refuse deletes. |
| OIDC | JWTs verified with PyJWT against the issuer's JWKS (`scruple/auth/oidc.py`): RS256 or ES256 only (`none`, HMAC and others refused before key lookup, so the public key cannot be used as an HMAC secret); `kid` required and the key type must fit the algorithm; `iss`, `aud`, `exp`, `nbf` checked with 60 s default clock skew; `exp`, `iat`, `iss`, `aud`, `sub` required. The JWKS is cached 10 minutes; an unknown `kid` refetches at most once per 30 s (rotation without letting random `kid`s hammer the issuer); an issuer outage keeps the cached keys. Tests use locally generated keys and a fake JWKS endpoint, no network (tests/test_auth_oidc.py). |
| Authorisation (RBAC) | Five roles: proposer (read, propose), service (read, shadow), approver (read, approve), risk_admin (read, approve, admin), auditor (read). Every route declares its permission with `require(...)`; `/health` and `/version` are the only public routes and the Stripe webhook is authenticated by its signature. An app-wide guard refuses (403) any route that declares nothing, so a new route is closed until it declares a permission. A test enumerates every route and calls each one anonymously (401) and with every other permission (403) (tests/test_auth_rbac.py). The OpenAPI schema needs `read`; `/docs` is off. |
| Multi-tenancy | Every stored row has a `tenant_id`, part of every primary key; every query filters on it (`scruple/storage/sql.py`). The tenant comes from the credential (key record or token claim), never from the request. Each tenant has its own rule registry, decision numbering and audit hash chain; another tenant's rules, decisions, reports and shadow state answer 404. Property tests generate one tenant's data and call every route with the other tenant's credentials, one key per role: always 403 or 404, or the caller's own data with no trace of the other's (tests/test_tenancy_api.py, tests/test_tenancy_storage.py). Postgres: alembic migration 0003; SQLite files from before tenancy are rebuilt once, rows and hashes unchanged. |
| Rate and size limits | Token bucket per credential: 600 requests a minute, burst 100 by default, per key overrides (`rate_per_minute`, `burst`); anonymous requests 120 a minute per client address; refused credentials 30 a minute per address, after which requests with credentials get 429 before any hash is computed. 429 carries `Retry-After`. Bodies over 2 MiB (or the key's `max_body_bytes`) get 413, from `Content-Length` or while streaming (tests/test_auth_ratelimit.py). Buckets are in memory per process behind a `RateLimitBackend` protocol; several workers each enforce their own share. |
| Separation of duties | Enforced on authenticated identities: `POST /rules` requires the caller to be the provenance agent, and the approver is the caller's own subject. No single key or token may hold both `propose` and `approve` (key creation refuses it, a token that grants both is refused). The rule's owner, its proposer and the learning agent can never approve its activation; approvals can require N of M named approvers, optionally one with a required role; grants and requests expire (default 72 hours and 7 days) (`scruple/gate/approval.py`, tests/test_gate_approval.py). Activation always needs a named human approver (docs/DECISIONS.md D-012). |
| Agent tool access | The gate MCP server does not expose approve or rollback (`scruple/mcp/gate_server.py`). |
| Secrets | The database password is accepted only from the environment, never from config files; a password inside a `database_url` read from a file is refused (`scruple/settings.py`, tests/test_settings.py). Pre-commit runs gitleaks and detect-private-key. |

### Seen on a running server

Real output, November 2026: `scruple keys` on a fresh SQLite file, then `curl`
against `scruple serve` on it, started with `SCRUPLE_RATE_LIMIT_BURST=3` to show
the limit. Key secrets are replaced by `<secret, shown once>` here; the command
prints the whole key once.

```
$ scruple keys create --db gate.db --subject examiner --role auditor
scr_68b9de35e89a5356_<secret, shown once>
created scr_68b9de35e89a5356 for examiner in tenant default. This is the only time the key is shown; store it in a secret manager.
$ scruple keys create --db gate.db --subject both-hats --role proposer --role approver
error: separation of duties: one credential may not hold both approve and propose
$ scruple keys list --db gate.db
scr_68b9de35e89a5356  active   examiner  auditor  last used never
scr_399b31bd10d71dfd  active   model-risk@demo.bank  approver  last used never

$ curl -i localhost:18431/rules
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer
{"detail":"authentication required: send an API key (X-API-Key) or a bearer token"}

$ curl -i -H "X-Actor: model-risk@demo.bank" localhost:18431/rules
HTTP/1.1 401 Unauthorized
{"detail":"authentication required: send an API key (X-API-Key) or a bearer token"}

$ curl -H "X-API-Key: $READER" localhost:18431/auth/whoami
{"subject":"examiner","tenant":"default","method":"api_key","credential_id":"scr_68b9de35e89a5356","roles":["auditor"],"permissions":["read"]}

$ curl -i -X POST -H "X-API-Key: $READER" localhost:18431/rules/base-fx-contest/rollback -d '{"reason":"drill"}'
HTTP/1.1 403 Forbidden
{"detail":"permission 'approve' required"}

$ for i in 1 2 3; do curl -s -o /dev/null -w "%{http_code} " -H "X-API-Key: $APPROVER" localhost:18431/rules; done
200 200 200
$ curl -i -H "X-API-Key: $APPROVER" localhost:18431/rules
HTTP/1.1 429 Too Many Requests
retry-after: 1
{"detail": "request rate limit exceeded; retry after 1 s"}

$ curl -i -H "X-API-Key: scr_0123456789abcdef_<43 wrong characters>" localhost:18431/rules
HTTP/1.1 401 Unauthorized
{"detail": "invalid API key"}

$ curl -i -X POST -H "X-API-Key: $READER" --data-binary @3MB.json localhost:18431/rules
HTTP/1.1 413 Request Entity Too Large
{"detail": "request body over 2097152 bytes"}
```

## 6. Software development

| question | answer |
|---|---|
| Testing | 5,388 tests in the default suite (counted on the November 2026 run; 188 of them in tests/test_auth_*.py and tests/test_tenancy_*.py) (ruff, ruff format, mypy strict, pytest), plus slow and Postgres suites. CI workflow `.github/workflows/ci.yml` runs lint, typecheck, tests with coverage, the demo, Postgres tests with migrations, a compose check and an image build. |
| Change control | Small commits; every commit passes `make lint typecheck test`. |
| Vulnerability scanning | Not set up (no dependency or image scanner in CI). |

## Not yet

Checked against the code on the date above. Each is either planned in docs/ROADMAP.md or
recorded in docs/DESIGN.md.

| gap | status |
|---|---|
| SOC 2 | Not started. |
| Penetration test | None yet, internal or external. |
| SAML, mTLS | Not built. Authentication is API keys and OIDC bearer tokens. |
| Shared rate limit store | Buckets are in memory per process; a Redis or database backend implements `RateLimitBackend` but none ships. |
| Tenant administration | Tenants are created implicitly by the first key or token for them; there is no tenant list, quota or offboarding command. Stripe webhook batches are not tenant-scoped (they are not persisted either). |
| Per-tenant genesis | Each tenant's audit chain starts from the same genesis hash. A copy of one tenant's rows into another tenant's id, by someone with raw database write access, would verify; anchor each tenant's head externally. |
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
