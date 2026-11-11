# Testing

How the scruple engine is tested, what each kind of test proves, and what it does not.
Everything the tests touch is synthetic, except the public CFPB sample in data/real/
and the vendored public fixtures in tests/fixtures/ (each with its licence).

## Running

```bash
make test            # default suite, parallel (pytest-xdist, -n auto --dist worksteal)
make test-slow       # the @pytest.mark.slow suites, hypothesis profile "thorough"
make test-postgres   # only the postgres-marked tests (throwaway local cluster)
uv run python -m tests.golden.regen [name ...]   # rewrite golden episodes, then review
uv run python -m tests.count_tests               # the table below
uv run python -m tests.load                      # load numbers (docs/PERFORMANCE.md)
```

Postgres tests run in the default suite whenever `initdb`/`pg_ctl` are installed (or
`SCRUPLE_TEST_POSTGRES_URL` is set); each xdist worker starts its own cluster on a
free port in its own temp dir, and every test uses its own schema. Hypothesis
profiles: `dev` (default, 50 examples, fresh each run), `ci` (derandomized),
`thorough` (1,000 examples, `make test-slow`); pick with `HYPOTHESIS_PROFILE`.

Wall time on the 4-vCPU development container, measured while other agents' test
runs shared the machine (load average 4 to 9 on 4 cores, so treat these as rough):

| run | wall | CPU (user) |
|---|---:|---:|
| before: 2,640 tests, serial `pytest` | 3 min 42 s | 3 min 38 s |
| same 2,640 tests, `-n auto` | 1 min 52 s | 3 min 37 s |
| now: 3,842 default tests (3,841 pass, 1 skip), `make test`, load average about 8 | 3 min 23 s | not recorded |

The default suite was parallel-safe as it stood: tests use `tmp_path`, in-memory
SQLite, per-test postgres schemas and a postgres cluster per worker on a free port.
Nothing needed fixing for xdist.

## Counts

Pasted from `uv run python -m tests.count_tests` (2026-11-09). A parametrised case
counts once per parameter, as pytest counts it (adapter contracts have one case per
documented status, reason, return code, type code and currency; audit tamper has one
per chain position and attack). "hypothesis or stateful" counts test functions driven
by hypothesis, each of which runs many examples.

| category | tests | hypothesis or stateful | slow | postgres |
|---|---:|---:|---:|---:|
| stateful properties (161, 162) | 8 | 6 | 0 | 0 |
| metamorphic (163) | 10 | 10 | 0 | 0 |
| golden episodes (164) | 27 | 0 | 0 | 0 |
| api fuzzing (166) | 22 | 15 | 0 | 0 |
| determinism (168) | 8 | 0 | 1 | 2 |
| load (169) | 4 | 0 | 3 | 0 |
| chaos / fault injection (170) | 17 | 0 | 2 | 4 |
| audit tamper (171) | 281 | 0 | 0 | 0 |
| adapter contracts (172) | 808 | 5 | 0 | 0 |
| end to end in processes (173) | 1 | 0 | 0 | 0 |
| storage backends | 162 | 0 | 0 | 65 |
| simulator | 990 | 40 | 1 | 0 |
| gate and statistics | 347 | 54 | 37 | 0 |
| verifiers and attacks | 22 | 0 | 0 | 0 |
| registry, audit, service | 43 | 0 | 0 | 0 |
| ingest adapters | 437 | 26 | 0 | 0 |
| agents and episodes | 319 | 3 | 0 | 0 |
| real data (CFPB) | 183 | 9 | 2 | 0 |
| ci, evidence packs | 89 | 0 | 0 | 0 |
| api, cli, settings, ops | 73 | 0 | 0 | 0 |
| other (demo, logging, version, deploy, alias) | 39 | 0 | 2 | 0 |
| **total** | **3890** | **168** | **48** | **71** |

3842 run by default (`make test`), 48 only in `make test-slow`; 6 of the property
tests are stateful machines.

## What each kind proves, and what it does not

**Example and unit tests** (most of the simulator, gate, ingest, agents, ci rows). One
input, one expected output, written by hand. They prove the cases someone thought of.

**Stateful properties** (`test_props_ledger.py`, `test_props_workflows.py`). Hypothesis
drives random sequences of operations and checks invariants after every step:
- ledger, across the card ledger, the batch `Simulator` (step, snapshot, restore,
  reset) and the `DisputeEngine` (actions, time, rewinds, fines): every entry sums to
  zero, every currency nets to zero, balances equal a recomputation from the journal,
  refused postings change nothing, a cardholder never holds more than was captured
  without a recorded violation (engine) or a verifier finding (batch simulator), and
  the event log replays to the identical state digest;
- workflows: the dispute lifecycle (both workflows), the rule registry against a
  model of `registry.state.ALLOWED`, and the promotion path (one-shot canary, staged
  ramp, kill switch, legacy and N-of-M approval with expiry, rollback) driven by any
  actor in any order: refused moves change nothing, every recorded status move is
  legal, activation only follows a passed canary with no unfinished ramp and, under a
  policy, a recorded quorum; integrity holds after every step.

They prove the invariants for the sequences hypothesis tried (25 to 40 machines of 20
to 30 steps by default), not for all sequences. Deep paths are reached less often than
shallow ones: the approval-mid-ramp bug below was found by a targeted probe after the
machine passed, and its regression test pins it.

**Metamorphic** (`test_metamorphic.py`). Scaling every amount by k scales debits and
costs by k (exactly in USD, within 2k to 4k minor units across FX floors; fees fixed)
and scales the bootstrap interval by k; permuting cases changes no result, aggregate,
finding count or bootstrap interval; relabelling ids changes only labels (batch
simulator; and the engine with the label-free `TruthIssuer`); escalation, a dominated
action, never improves the optimum; a never-matching rule and a `RequestInfo` call
change nothing. They do not cover the e-value statistics (order-dependent by design)
or the canary split (hashes ids by design), and the seeded issuer's draw is keyed by
case id on purpose.

**Golden episodes** (`tests/golden/`, `test_golden_episodes.py`). Canonical JSON traces,
compared byte for byte: the batch simulator for three seeds, the dispute engine played
by a scripted agent per workflow for three seeds (events, actions, outcomes, digest),
the full gate promotion for two seeds (audit chain, decisions, registry, head) and an
N-of-M approval. Any drift fails with a diff. They prove nothing is changing by
accident; they do not say the recorded behaviour is right, and an intended change
needs `tests.golden.regen` and a reviewed diff.

**API fuzzing** (`test_api_fuzz.py`, schemathesis 4 against the app in process). Every
route, inputs generated from its own OpenAPI schema, valid and invalid: no 5xx, bodies
and headers match the documented schema, invalid input is rejected, every 4xx is JSON
with a `detail`. Excluded checks are listed in the module docstring with the reason.
It does not reach states that need a multi-step setup (an approval after a passed
canary) except by chance, and it trusts the schema's own claims about types.

**Determinism** (`test_determinism.py`). The golden traces, a CI scorecard, an evidence
pack and sharded generation hash identically in fresh interpreters with different
`PYTHONHASHSEED` (two seeds by default, six in the slow suite) and in the test process;
the promotion and approval traces are identical on SQLite in memory, SQLite on disk
and PostgreSQL. It does not cover other Python versions or platforms (the project pins
3.12) or the wall clock (`SystemClock` is never used in these traces).

**Load** (`test_load.py`, `tests/load.py`). Rollouts per minute through the episode SDK
and the episode CLI, requests per second against a real uvicorn process. Numbers in
docs/PERFORMANCE.md. Thresholds are far below what was measured so a busy machine does
not flake; they catch an order-of-magnitude regression, not a 20% one.

**Chaos** (`tests/faults.py`, `test_chaos.py`). A `FaultyStorage` wrapper fails the k-th
storage call (an error on even k, a `KeyboardInterrupt` on odd k) for every k of ten
state-changing operations, on SQLite and (every third call) PostgreSQL, and a child
process is killed with `os._exit` mid-step on a SQLite file. After each fault the stored
state is byte-identical to the state before or the state after, integrity passes, and a
retry succeeds. It proves atomicity at the `Storage` interface; it does not simulate
torn writes inside the database engine (SQLite's journal and PostgreSQL's WAL are
trusted), network partitions to PostgreSQL, or concurrent writers.

**Audit tamper** (`test_audit_tamper.py`). A real 19-entry chain, attacked at every
position: edit each field, edit and re-hash, delete, delete and renumber, insert a
forged or a copied entry, swap neighbours, rewrite the whole tail; and the same on the
database file with raw SQL, and on an evidence-pack segment. Verification fails and
`first_bad_seq` names the first untrusted position; attacks that are internally
consistent by construction (the last entry, a full tail rewrite) are caught only by the
anchored head, which is what the tests assert. Without an anchor kept outside the
system, truncation and tail rewrites are undetectable; that is a property of hash
chains, not a gap in the tests.

**Adapter contracts** (`test_adapter_contracts.py`, extending `test_ingest_*.py`). Every
documented Stripe dispute status and reason, Adyen dispute event and dispute status,
NACHA return reason and transaction code, BAI2 type code and ISO 4217 currency goes
through the adapter's public entry point to a definite result; every undocumented value
is refused with an `IngestError` (Adyen reports non-dispute events as skipped, by
design; NACHA keeps unknown return codes with no title, by design). The vendored specs
are subsets fetched on a date (see each fixture README): the tests say nothing about
values those providers added later. camt.053 entry status (`Sts`) is kept verbatim and
not validated; no vendored schema lists its codes here, so it is not tested as a
closed set.

**End to end in processes** (`test_e2e_processes.py`). `scruple ci run` blocks a bad rule
and gates a good one into a temp SQLite file; a real `scruple serve` process refuses
the owner's approval and accepts the approver's; a separate `scruple rollback` process
rolls it back while the server runs; `scruple ci evidence` exports the JSON and PDF
pack, which verifies; `scruple audit verify` passes against the head the server
reported and fails after a raw edit of the file. It runs one path; the other paths are
covered by in-process tests.

## Bugs these tests found (each fixed with a regression test)

| found by | bug | fix commit message |
|---|---|---|
| ledger state machine | `DisputeEngine.rewind` accepted a cut inside a step (e.g. between an issuer reply clearing the due date and the resolution); the next `sync` crashed on `assert st.due is not None` | `[!] refuse dispute rewinds that cut inside a step` |
| workflow probe | right after `start_canary_ramp`, the latest canary decision was still the one-shot pass, so `approve` (and approval requests) skipped the whole staged ramp | `[!] refuse approval while a canary ramp is unfinished` |
| metamorphic | the paired bootstrap resampled the non-zero differences in arrival order, so the same evidence in another order gave another interval | `[!] make the bootstrap interval independent of case order` |
| schemathesis | a rule version above 2^63 - 1 in a path reached SQLite and raised `OverflowError` (HTTP 500; CLI traceback) | `[!] reject out-of-range rule versions instead of crashing` |
| chaos | `evaluate`, `approve` and `rollback` wrote in several transactions, so a failure part-way left a version in shadow with no decision, or an activation / rollback decision whose transition never happened | `[!] make evaluate, approve and rollback one transaction each` |
| audit tamper | after deleting or swapping entry i, verification reported i+1 as the first bad entry, implying entry i was fine | `[!] point audit verification at the first untrusted position` |
| audit tamper | a tampered payload (`{}`) made `verify_integrity` raise `KeyError` (500 on `/audit/verify`) instead of reporting it | `[!] report tampered audit payloads instead of crashing` |
| end to end | `scruple audit verify` imported the baseline rules before verifying, appending six audit entries to the evidence it was checking | `[!] keep audit verify read-only` |

## Not covered

- Mutation testing (roadmap 165) and exploit regressions (167): later.
- The verifier rewrite and `scruple/agents/env.py` in progress elsewhere: the golden
  gate traces and the episode load numbers will move when that lands; regenerate the
  goldens and review the diff.
- Real processor traffic, real bank files beyond the vendored samples, and any
  production deployment: everything here is synthetic or a public fixture.
