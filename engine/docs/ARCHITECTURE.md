# Architecture

How the scruple engine is put together, one layer per section, bottom-up in the
order data flows through it. docs/DESIGN.md has the reasoning behind the gate; this
file is the map. All data is synthetic.

```
 interfaces    api (FastAPI)          cli (argparse, demo)
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

## 8. Interfaces (`scruple/api/`, `scruple/cli/`)

```
 HTTP request
   │ RequestIDMiddleware      X-Request-ID in, out, and on every log line
   │ reject_unknown_inputs    unknown query params / body on bodiless route -> 422
   │ Pydantic body models     extra="forbid", nested all the way down -> 422
   ▼
 route ─► AgentGate ─► response model
```

Both front ends call `AgentGate` and nothing else. Identity is the `X-Actor` header,
trusted as-is: a stub until real authn exists. `GET /version` and `scruple version`
report the package version, commit and build date. The CLI `propose` command reads
its file through the same `RegisterIn` model as `POST /rules`.

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
ids, real authn, signed audit checkpoints. The demo (`scruple demo`) still writes
SQLite files.
