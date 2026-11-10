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
ids, SAML and mTLS, a shared rate-limit store, signed audit checkpoints. The demo (`scruple demo`) still writes
SQLite files.
