# Architecture

How the scruple engine is put together, one layer per section, bottom-up in the
order data flows through it. docs/DESIGN.md has the reasoning behind the gate; this
file is the map. All data is synthetic.

```
 interfaces    api (FastAPI) + auth   cli (argparse, demo)
                     \                 /
 service              AgentGate (service.py)  <- the only place verdicts change state
                 /        |          |          \
 gate        engine   registry    audit      reports
             (pure)   (state m.)  (hash chain)
               |           \        /
 verifiers   ledger . outcome . policy . adversarial
               |
 simulation  generator -> simulator -> ledger
               |
 domain      RuleSpec, Condition, observable-field whitelist
 ------------------------------------------------------------------
 storage     Storage facade -> SQLite | Postgres   (append-only in the database)
 operations  settings . logging . version . docker . ci
```

Dependencies point down only: `domain` <- `registry`/`sim` <- `verifiers` <- `gate`
<- `service` <- `api`/`cli`. Storage sits beside them: only `registry`, `audit` and
`service` use it, and only through the `Storage` protocol.

## 1. Domain: the rule language (`scruple/domain.py`)

```
 RuleSpec
 ├── description      (text, ignored by the hash of behaviour)
 ├── conditions[1..10]
 │     └── Condition(field ∈ FIELD_TYPES, op, value)   type-checked per field
 ├── action           accept | contest | escalate
 └── priority         0..1000, lower runs first, first match wins
```

A learned rule is a predicate over the observable fields of a case plus an action.
The whitelist (`RuleField`, `FIELD_TYPES`) is a security control: ground truth,
scenario labels and ids are not addressable, so a rule cannot leak labels or
memorise cases. Property tests check that `matches` never raises on any record the
language can express (tests/test_properties.py).

## 2. Simulation (`scruple/sim/`)

```
 seed ──► generate_cases ──► [DisputeCase ...] ──► Simulator.run(policy)
          (one Random(seed))      │ view()             │ per case:
                                  ▼                    │  policy.decide(view)
                         observable fields only        │  outcome_for(case, action)
                                                       ▼
                                   RunResult(results, journal) ◄── Ledger.post
                                                                   (each entry sums to 0)
```

Every random source takes an explicit seed, so one seed and profile always give
the same cases and the same run digest. The ledger is double-entry: an entry that
does not balance is refused before it is recorded. Snapshots let a run restart
from any case.

## 3. Verifiers (`scruple/verifiers/`)

```
 RunResult ──┬──► ledger     re-derives balances from postings, reconciles results
 cases    ───┼──► outcome    regret vs the hindsight-optimal action
             ├──► policy     hard per-case constraints
             └──► adversarial attacks that fool a naive strawman, not the gate
                          │
                          ▼
                 VerifierReport(findings, metrics)
```

Verifiers are anchored to the journal and the ground truth, never to what the
simulator or the agent says about itself. Each verifier ships with an attack it
must stop (docs/VERIFIER_FINDINGS.md).

## 4. Gate (`scruple/gate/`)

```
 candidate ──► shadow replay (held-out seed, paired with baseline)
                 verifiers · paired bootstrap CI · slices · golden suite
                 │
        BLOCK ◄──┼──► HOLD
                 ▼
               PROMOTE ──► canary (hash split of simulated live traffic)
                             │
                ROLLBACK ◄───┼──► HOLD
                             ▼
                        CANARY_PASSED ──► human approval ──► ACTIVE
```

`engine.py` is pure: cases and policies in, a `Verdict` out, no I/O. That makes
every decision replayable and easy to test.

## 5. Registry (`scruple/registry/`)

```
 candidate ──► shadow ──► canary ──► active ──► retired
      │           │          │          ▲
      └───────────┴──────────┴─► rejected│ rollback restores the previous
                                         └ version via its rollback_to pointer
```

A HOLD in shadow sends the version back to candidate. Versions are
content-addressed (`content_hash` of the spec). Re-submitting the same
content or the same behaviour reworded is refused. The transition table is
hardcoded in `state.py`; privileged moves (activate, rollback) have their own
methods. Only `status` and `rollback_to` ever change on a stored version.

## 6. Audit (`scruple/audit/`)

```
 entry n:  { seq, at, event_type, actor, payload, prev_hash = hash(n-1) }
           entry_hash = sha256(canonical_json(body))

 genesis ─► e1 ─► e2 ─► ... ─► head      (anchor `head` outside the system)
```

Editing, deleting or reordering any entry breaks every hash after it. Truncation
keeps the chain valid, which is why `verify` takes an externally anchored head.

Anchoring (`scruple/audit/anchor.py`, `timestamp.py`, `der.py`): `scruple audit anchor`
hashes a four-line statement (`scruple-audit-anchor/1`, tenant, entry count, head) and
either gets an RFC 3161 token for it from a time-stamping authority (`--tsa-url`,
stored as JSON with the base64 token) or commits it to a git repository
(`--git-repo`, never pushed). `scruple audit verify-anchor` re-verifies the token
offline against `--tsa-cert` (signature, imprint, signed attributes, signing-certificate
attribute, critical timeStamping key usage, validity at genTime, trust one level up)
and checks that entry N of the current chain still has the anchored head. Not checked:
revocation (CRL/OCSP), longer chains, RSA-PSS. A git anchor counts only once pushed to
a remote the operator cannot rewrite. Tests use a local TSA and check interop with
`openssl ts` both ways (tests/test_audit_anchor.py).
`AgentGate.verify_integrity` also replays the log against the registry and the
stored decisions, so a direct edit to a status or a decision is reported.

## 7. Service (`scruple/service.py`)

```
 propose ─► Registry.register_candidate ─► audit
 evaluate ─► gate.engine (pure) ─► Verdict ─► _record ─┬─► Registry.transition
                                                       ├─► audit "decision.recorded"
                                                       └─► storage.insert_decision
                                    (one storage.transaction(): all or nothing)
```

`AgentGate` is the only code that turns a verdict into a state change, and every
change it makes is audited inside one transaction. `AgentGate.open(location)` picks
the storage backend from the location string.

## 8. Interfaces (`scruple/api/`, `scruple/auth/`, `scruple/cli/`)

```
 HTTP request
   │ RequestIDMiddleware      X-Request-ID in, out, and on every log line
   │ AuthMiddleware           credential -> Principal (401 if refused)
   │   ├ API key              X-API-Key or Bearer scr_...  (keys.py, scrypt, cached check)
   │   ├ OIDC bearer token    JWT vs the issuer's JWKS      (oidc.py, RS256/ES256)
   │   ├ console session      signed cookie after SAML SSO  (session.py, /console/* only)
   │   ├ rate limit           token bucket per credential   (429 + Retry-After)
   │   └ body size            Content-Length and streamed   (413)
   │ guard_route              the route's declared protection, first (401/403);
   │                          a route that declares none is closed (403)
   │ reject_unknown_inputs    unknown query params / body on bodiless route -> 422
   │ Pydantic body models     extra="forbid", nested all the way down -> 422
   ▼
 route ─► tenant_gate(principal) ─► AgentGate for the caller's tenant ─► response
```

Both front ends call `AgentGate` and nothing else. The caller's identity (the
audit actor) and tenant come from the credential, never from a header or body the
caller chooses. `X-Actor` is trusted only in the development-only mode
(`SCRUPLE_AUTH_DEV_ACTOR_HEADER=true`, off by default, warned about at start and
flagged on every response). The command line is the local admin of one tenant
(`SCRUPLE_TENANT`, default `default`): whoever can run it can open the database
anyway. Keys are managed with `scruple keys create|list|revoke`.

SAML 2.0 sign-on for the console sits alongside OIDC (`scruple/auth/saml.py`,
`saml_routes.py`, `session.py`; on when the five `SCRUPLE_SAML_*` endpoint settings
and `SCRUPLE_CONSOLE_SECRET` are set). SP-initiated, HTTP-POST binding:
`GET /auth/saml/login` (public) redirects to the IdP with an AuthnRequest and a
signed, expiring RelayState carrying the request id; `POST /auth/saml/acs`
(`signed("saml")`) checks the response with python3-saml in strict mode (assertion
signed by the configured IdP certificate, SHA-256 or stronger, Issuer, Audience,
Destination, Recipient, time window, InResponseTo, one assertion, each assertion id
once) and maps NameID (or an attribute), a tenant attribute and a roles attribute
(optionally renamed by `SCRUPLE_SAML_ROLE_MAP`) through the same role table as API
keys. The result is a signed, expiring `scruple_session` cookie (HttpOnly,
SameSite=Lax, Secure on https, path `/console`) that the authenticator accepts on
console paths only, never on the JSON API. XML signatures are verified by xmlsec1
through python3-saml; nothing in scruple parses or verifies XML signatures by hand.
`GET /auth/saml/metadata` serves the SP metadata. Not built: signed AuthnRequests,
encrypted assertions, single logout, a replay cache shared across processes.
Tests (`tests/test_auth_saml.py`) use a locally generated IdP key and certificate
and xmlsec-signed responses, and check unsigned, wrongly signed, tampered, wrapped
(XSW), SHA-1, wrong audience/issuer/recipient/destination, expired, unsolicited,
mismatched, replayed and entity-expansion responses are all refused.

Rate-limit buckets are per process by default. `SCRUPLE_RATE_LIMIT_BACKEND=database`
keeps them in `rate_limit_buckets` on the gate database (`scruple/auth/ratelimit_db.py`,
alembic 0008): on Postgres a row lock per bucket makes every replica share one limit,
timed by the database server's clock; on SQLite the processes that open the same file
share it.

`GET /version` and `scruple version` report the package version, commit and build
date. The CLI `propose` command reads its file through the same `RegisterIn` model
as `POST /rules`.

### Protecting a route (for new routers: console, observability, ...)

Every route must declare what it needs; `scruple.auth` gives three markers, each one
line:

```python
from scruple.auth import CurrentPrincipal, Permission, Principal, public, require

router = APIRouter(prefix="/console", dependencies=[require(Permission.READ)])  # whole router

@router.post("/thing", dependencies=[require(Permission.ADMIN)])                # one route
def thing(p: CurrentPrincipal): ...                                             # p.subject, p.tenant

@router.post("/other")
def other(p: Annotated[Principal, require(Permission.PROPOSE)]): ...            # check and get

@router.get("/metrics-health", dependencies=[public()])                         # no credentials
```

`signed("name")` marks a route that verifies a request signature itself (the
Stripe webhook). Permissions are `read`, `propose`, `shadow`, `approve`, `admin`;
roles grant them (`scruple/auth/rbac.py`). A route that declares nothing answers
403 to everyone, and tests/test_auth_rbac.py enumerates every route (included
routers too, via `scruple.auth.deps.iter_routes`) and fails on an undeclared one,
so add the marker and, for a new public route, add it to `PUBLIC` in that test with
a reason. Use `Gate` (`scruple.api.app.tenant_gate`) for the caller's tenant's
`AgentGate`; never hold a gate of your own across requests. Static mounts are not
routes: the middleware still authenticates them, but nothing checks a permission,
so serve no data from a mount.

## 9. Storage (`scruple/storage/`)

```
              Storage (facade, one transactional object)
      ┌─────────────┬──────────────┬───────────────┬──────────────┐
 Transactional  RuleVersion    Audit          Decision        (protocols,
                Repository     Repository     Repository       base.py)
                     │              │              │
               SQLRuleVersions  SQLAudit     SQLDecisions      (sql.py, portable SQL)
                     └──────────────┼──────────────┘
                             Connection protocol
                       ┌────────────┴────────────┐
                SQLiteConnection          PostgresConnection
                file or :memory:          psycopg 3, advisory write lock
                BEGIN IMMEDIATE           schema per store (optional)
                triggers                  triggers + non-owner role grants
                                          alembic migrations
```

- One repository protocol per aggregate; the `Storage` facade composes them so a
  status change, a decision and its audit entries commit together.
- Both backends encode rows identically (ISO 8601 timestamps and JSON as text), so
  the bytes that were hashed are the bytes read back. The same contract suite runs
  against SQLite (file and memory) and Postgres (tests/storage/test_contract.py).
- Append-only is enforced in the database. Triggers refuse UPDATE/DELETE on audit
  events and decisions, refuse content changes and DELETE on rule versions, and on
  Postgres also refuse TRUNCATE. On Postgres, run the service as a role that does
  not own the tables (`app_role_grants`): it cannot drop the triggers, and its
  column grants allow UPDATE only on `status` and `rollback_to`.
- Driver errors become `DuplicateKeyError`, `ImmutableHistoryError` or
  `StorageError`, so callers never import a driver.
- Tenancy: `rule_versions`, `audit_events` and `decisions` carry `tenant_id`, part
  of each primary key; a store is bound to one tenant and every statement filters
  on it (`SQLStorage.for_tenant(t)` is a view of the same connection for tenant
  `t`). Each tenant has its own registry, decision numbers and audit hash chain.
  `api_keys` holds keys for every tenant (looked up by id; the record names the
  tenant); only `last_used_at` and one `revoked_at` may change. Postgres reaches
  this through migrations 0002 (keys) and 0003 (tenancy); a SQLite file written
  before tenancy is rebuilt once on open with every row in tenant `default`, rows
  and hashes unchanged.
- Per-tenant audit genesis (`scruple.audit.models.genesis_hash`): a tenant's chain
  starts from `SHA-256("scruple-audit-genesis" || 0x00 || tenant_id)`, so rows copied
  into another tenant fail verification at the first entry (tests/test_audit_tenant_
  genesis.py). Migration path: tenant `default` keeps the old all-zero genesis, so
  every chain written before tenancy (all of which migration 0003 put in `default`)
  verifies unchanged and nothing is rewritten. No other tenant could have a chain
  before this change, since tenancy and the per-tenant genesis arrived together. The
  cost: the default chain is bound by its anchored head, not by its genesis (a
  default chain copied into another database's default tenant is not told apart).
- What is per tenant, and what is not:

  | table / state | tenant | why |
  |---|---|---|
  | rule_versions, audit_events, decisions | `tenant_id` in the key | each tenant's own registry and chain |
  | imported_shadow_*, episodes, episode_calls, jobs, job_events, console_filter_events, usage_events, billing_pushes | `tenant_id` column | every read and write filters on it; jobs run in their tenant (`JobContext.tenant`) |
  | retention_policies, retention_purges | `tenant_id` column, read across tenants | `scruple retention apply` is an operator command over every tenant's policy; each purge is audited in the purged tenant's own chain |
  | api_keys | `tenant_id` column, looked up across tenants | a key is found by its id before the tenant is known; the record names the tenant |
  | case store (`CaseStore`, a separate SQLite file) | one tenant per file | webhooks authenticate by the provider's signature, not a key, so the signing secret picks the tenant (`tenant=secret` entries in `SCRUPLE_STRIPE_WEBHOOK_SECRETS` / `SCRUPLE_ADYEN_HMAC_KEYS`; a bare secret is `SCRUPLE_INGEST_TENANT`'s) and each tenant writes its own store (`SCRUPLE_CASE_STORE`, `SCRUPLE_CASE_STORES`; worker `--case-store-tenant`); `cases` and `movements` carry no tenant column |
  | console reference runs and backtest reports | shared | synthetic reference data loaded at start, not tenant data; rules, decisions and approvals in the console are the caller's tenant's |
  | MCP simulator episodes | tenant `default` | the MCP server has no authentication; episodes are synthetic |
  | live shadow recorders | in memory, keyed by tenant | not stored (see docs/DEPLOY.md, known gaps) |
- Locations: `:memory:`, a path or `sqlite:///path` for SQLite;
  `postgresql://...` for Postgres. Postgres schema changes go through alembic
  (`scruple/storage/migrations`); a test checks that `upgrade head` builds the same
  catalog as the backend's own DDL.

## 10. Operations (settings, logging, packaging, CI)

```
 defaults < scruple.toml ($SCRUPLE_CONFIG) < SCRUPLE_* env < explicit overrides
                         └─ secrets refused here: env only

 docker compose:  postgres:16 ◄── api (non-root, uv, read-only fs)
                                   entrypoint: migrate to head, then `scruple serve`

 CI: lint · typecheck · test (+coverage ≥ 90%) · postgres (service container)
     · docker build · slow (weekly, thorough hypothesis profile)
```

- `scruple/settings.py`: typed loader. `database_password` only from the
  environment; `Settings.database_dsn()` merges it into a Postgres URL.
- `scruple/logging.py`: one JSON object per line on the root logger, with the
  request id from a context variable.
- `scruple/version.py`: version from package metadata, commit and build date
  from `SCRUPLE_BUILD_COMMIT` / `SCRUPLE_BUILD_DATE` (set by the Dockerfile) or git.
- Make targets: `test`, `test-slow`, `test-postgres`, `bench`, `demo`, `release`
  (build plus checksums, no publishing), `db-up`, `db-down`, `db-migrate`.
- Pre-commit: ruff, ruff-format, mypy (local), gitleaks.

Not yet built: connection pooling (one connection per store, behind a lock), typed
ids, SAML and mTLS, signed audit checkpoints (the shared rate-limit store is built: section 8). The demo (`scruple demo`) still writes
SQLite files.

### Usage metering (`scruple/usage/`)

Append-only `usage_events` per tenant (alembic 0012), unique by `(tenant_id,
event_key)`. Events are derived from the tenant's gate decisions: `gate_run` (one per
shadow, canary or assurance decision), `gate_cases` (the cases a shadow or canary
replayed, as the decision records them) and `rollout` (one per activation), keyed
`<decision id>:<meter>` and timed by the decision's `created_at`, so
`UsageStore.sync(gate)` is idempotent and a period's total does not depend on when it
ran. `aggregate` totals per meter per UTC day or month. `scruple usage sync|report`
on the command line; `GET /usage` and `GET /usage/events` on the API, both `admin`
and both for the caller's tenant only (they sync first). Billing reads the
aggregates; metering prices nothing.

### Billing hooks (`scruple/billing/`)

Usage reaches Stripe as billing meter events (`POST /v1/billing/meter_events`, field
names and limits from the vendored subset of Stripe's OpenAPI spec in
`tests/fixtures/stripe_billing`, API version 2026-09-30.endive). A subscription bills
them through a metered price on a Stripe meter with the same event name; the meters,
prices and subscriptions are set up in Stripe. `billing.toml` (`[billing]`) maps
tenants to Stripe customers and scruple meters to event names; the key comes from
`SCRUPLE_STRIPE_BILLING_KEY` only. Per tenant, meter and UTC day, the total not yet
in the append-only `billing_pushes` ledger (alembic 0013) is sent as one event whose
`identifier` and `Idempotency-Key` derive from (tenant, meter, day, running total):
re-runs send nothing, late usage sends only the difference, and a retry after a lost
answer is deduplicated by Stripe. Days older than `lookback_days` (at most 34; Stripe
takes timestamps up to 35 days old) are reported stale, never dropped silently. Test
mode by default: live keys are refused unless `mode = "live"` and `--allow-live`, and
a live-mode answer to a test key is an error. `scruple billing plan` is the dry run;
`scruple billing push` sends. Tests run against a fake transport; nothing in the suite
calls Stripe.

## 11. Persistence of runtime state, and jobs

Everything a running service learns is stored, so a restart (or a second replica)
loses nothing. Every new table carries `tenant_id` (default `"default"` until
authenticated tenants are wired through the API) and is append-only unless stated.

```
 gate database (SQLite file or Postgres; alembic 0004-0007)
 ├─ imported_shadow_events     accepted batches, replayed to rebuild a recorder
 ├─ imported_shadow_summaries  the summary after every batch (aggregate)
 ├─ episodes, episode_calls    simulator episodes for the API / MCP server
 ├─ jobs (mutable state)       the queue: queued → running → succeeded|failed|cancelled
 ├─ job_events                 every submit, claim, retry, timeout, cancel, result
 └─ retention_policies, retention_purges

 case store (one partner's SQLite file, scruple.ingest.store)
 ├─ cases, movements, ...      imports, versioned
 ├─ webhook_events             event id, time, payload hash (idempotency key)
 └─ webhook_case_events        each case an event carried, applied or stale
```

**Imported shadow** (`scruple/storage/imported_shadow.py`). The recorder in
`scruple/gate/shadow_imported.py` stays pure. `DurableImportedShadow` appends each
batch the recorder accepted (canonical JSON plus its SHA-256) and the summary after it,
inside the backend's write transaction, after catching up on batches other processes
wrote. A fresh process rebuilds by replay: `record` and `observe` are deterministic
and all-or-nothing, so replay reproduces the state exactly. A failed write drops the
in-memory recorder, so memory can never run ahead of storage. API:
`GET /shadow/rules/{id}/versions/{v}/imported/history` lists the stored summaries.
CLI on the same store: `scruple shadow imported record|observe RULE V FILE --actor A`
(the API's batch bodies, `-` for stdin) and `scruple shadow imported summary|history
RULE V [--json]`; reads never write.

**Webhooks** (`scruple/ingest/api.py`, `CaseStore.write_webhook`). An accepted Stripe
or Adyen delivery becomes one ingest run. Idempotent by (tenant, source, event id):
a redelivery, including one after a restart, stores nothing. Out of order safe: a case
becomes current only if no newer webhook event already applied to it; a late, older
event is kept with `applied = 0`. If the store write fails the route answers 503 and
the id is forgotten, so the provider's retry is processed. Adyen items have no event
id; scruple uses `pspReference:eventCode:eventDate` (ASSUMPTION, see the module).

**Episodes** (`scruple/agents/store_db.py`). The CLI keeps its JSON file store; the
MCP server (`scruple mcp sim --episode-db ...`) uses `DbEpisodeStore`, and so can any
API route (`DbEpisodeStore.on(gate.storage)`; none is mounted yet), with the same
semantics: replay of the call log, refused calls logged, calls on a closed
episode not logged. Calls run in the write transaction, so several processes can
drive one episode; each caches live episodes and replays only when another process
appended calls.

**Jobs** (`scruple/jobs/`). Long work (gate evaluation on the full case count,
backtests, bench scoring) no longer runs inside a request.

```
 POST /jobs ──► jobs row (queued) ◄── claim ── scruple worker ──► child process
 GET /jobs/{id}      ▲    │ heartbeat (lease) every lease/3          (handler)
 POST /jobs/{id}/cancel   │ succeed | fail → retry with backoff | cancelled
                          └ fenced on (lease_owner, attempt)
```

- Claiming: Postgres `FOR UPDATE SKIP LOCKED` on a connection without the gate's
  advisory lock, so workers never take the same job and never wait on each other;
  SQLite serialises claims with `BEGIN IMMEDIATE`.
- Crash recovery: a worker heartbeats its lease; if it dies, the lease expires and the
  next claim takes the job as a new attempt. Every write is fenced on the lease owner
  and attempt number, so a stalled worker that wakes up writes nothing.
- Retries: exponential backoff (base x 2^(attempt-1), capped, no jitter so schedules
  reproduce under an injected clock) until `max_attempts`; `PermanentJobError` (bad
  params, a sealed benchmark) fails at once.
- Timeouts and cancellation: the default `process` isolation runs each job in a
  spawned child that is terminated on timeout or cancel; `thread` isolation (tests)
  abandons the thread and discards its result.
- Idempotency keys (`Idempotency-Key` header or body) are unique per tenant; the same
  key with a different submission is a 409.
- Results are stored on the job with their SHA-256. A gate evaluation or backtest is
  linked to its rule version by one `job.succeeded` audit entry (written once per job,
  even across retries), so it appears in that version's evidence pack audit segment.
  A gate-evaluation retry that finds the version already evaluated (the crash came
  after the commit) returns those decisions with `resumed: true`.
- Submitted jobs never name code or files: bench agents are the built-in ones, and a
  backtest reads either a synthetic history or the case store the worker was started
  with (`--case-store`).

**Retention** (`scruple/storage/retention.py`). A per-tenant policy (days; the latest
one set wins; setting one is audited) deletes row-level case data older than the
cutoff: imported-shadow events in the gate database, and, in the tenant's case store,
every version of each case opened before the cutoff, its movements and webhook case
bodies. Kept: the audit log, decisions, rule versions, imported-shadow summaries, run
reports, webhook event ids and hashes (so redeliveries stay refused), jobs.

How the audit chain stays verifiable: no audit entry is deleted or edited, so the
chain recomputes unchanged, and each purge appends a `retention.purged` entry naming
tenant, table, cutoff, row count and a SHA-256 digest over the deleted rows' keys and
body hashes. A deletion is therefore itself on the tamper-evident record, and a backup
can be checked against the digest. Audit payloads never contain case rows, so no entry
refers to purged data. The append-only triggers stay for every other path: SQLite
drops and recreates the one DELETE trigger inside the purge transaction; on Postgres
the trigger allows a delete only when the transaction set `scruple.retention =
'purge'` (TRUNCATE stays refused). That setting guards against stray SQL, it is not an
access control: grant DELETE on `imported_shadow_events` only to the role that runs
retention (`app_role_grants` does not grant it).

Commands: `scruple worker [--db URL] [--case-store FILE] [--once]`,
`scruple retention set --tenant T --days N`, `scruple retention apply [--case-store FILE]`,
`scruple retention schedule [--day D]`, `scruple worker --schedule-retention`,
`scruple mcp sim --episode-db URL`.

Scheduled retention (`scruple/jobs/retention.py`): one `retention_apply` job per tenant
with a policy per UTC day, idempotency key `retention-apply:<day>`, so any number of
workers or cron runs queue it once. The job applies its tenant's policy as of 00:00 UTC
that day (a retry deletes nothing more), refuses a future day, and is not submittable
over HTTP (not a `JobKind`).

A decision re-sent for a case whose imported-shadow events were purged is refused:
the purge keeps each decision's key as a SHA-256 of (tenant, rule, version, case id)
in the append-only `imported_shadow_keys` (alembic 0009), never the case id.

Known limits: re-importing an old export
file brings purged cases back; a case store is one tenant's file, so its `cases` rows
carry no tenant column. Retention does not cover episodes (synthetic) or job results
(no case rows by construction). Migrations 0004-0007 follow
auth (0002 api keys, 0003 tenancy) in one linear chain with a single head.
