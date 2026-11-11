# Benchmark and experiments

Everything here runs on synthetic dispute episodes from the scruple simulator. The
numbers describe our simulator and our reference policies, not real disputes.

## Benchmark v1 (frozen)

`scruple bench` (code: `scruple/bench/`). 10,000 dispute episodes from the sealed
held-out seed namespace (`scruple.sim.splits`): case `i` is the first case of
held-out seed index `1_000_000_000 + i` under the episode mix, with a salted id
prefix so no case id an agent sees carries the seed. Only
`results/bench/v1/manifest.json` is published: the commitments (salt, seeds, a
digest per block of 500 cases and one over all cases, ground truth included), the
case-family, difficulty, stage and network mix, and the reference scores. Its
content hash names the benchmark:

    manifest hash 1286baf3be84b4f9aa7af0606f4434897cf97db4a11c3db548d9549d2cb13f91

The cases regenerate from the salt (`SCRUPLE_HELDOUT_SALT`, environment only) and the
manifest; `scruple bench verify` regenerates all 10,000 and checks them, and `score`
checks every block it plays, so a wrong salt or a changed generator is refused rather
than scored. The salt is held by the owner, outside the repository. v1 is tied to
the engine that built it (`engine_version`, `mix_hash`): a change to the generator
that alters any case means a new version, not an edit (`tests/test_bench_v1.py`).

```
$ uv run scruple bench show
scruple-dispute-bench v1: 10,000 synthetic held-out dispute cases
manifest hash 1286baf3be84b4f9aa7af0606f4434897cf97db4a11c3db548d9549d2cb13f91
engine 0.1.0; block 500; salt commitment 49fb48c226405680
families: buyers_remorse 1977, cardholder_mistake 577, friendly_fraud 2791, merchant_error 625, service_failure 1840, true_fraud 2190
difficulty: aligned 7908, misleading 1304, uninformative 788
opening stage: chargeback 8050, inquiry 1950
valid claims: 4655

reference scores (95% intervals):
| agent | cases | reward | gate pass | cost (USD minor) | regret vs evidence | correct | win | hard failures |
|---|---|---|---|---|---|---|---|---|
| evidence | 10000 | 0.970 [0.969, 0.971] | 1.000 [1.000, 1.000] | 8272 [8062, 8481] | 0 [0, 0] | 0.779 [0.771, 0.787] | 0.451 [0.441, 0.461] | - |
| always_accept | 10000 | 0.868 [0.865, 0.871] | 0.985 [0.982, 0.987] | 12746 [12492, 13001] | 4475 [4266, 4683] | 0.445 [0.435, 0.454] | 0.000 [0.000, 0.000] | INV-01 155 |
| oracle | 10000 | 0.955 [0.952, 0.957] | 0.985 [0.982, 0.987] | 8667 [8451, 8883] | 395 [291, 498] | 0.857 [0.850, 0.863] | 0.412 [0.402, 0.422] | INV-01 155 |

Reference rows rebuilt 2026-11-11 (`scruple bench build --version 1`, 466 s with 2
processes) after the refund-race engine fixes (F-11 in docs/VERIFIER_FINDINGS.md),
the CAL-01 tie fix (F-12) and the EVD-02 split (D-019). The cases did not change: the
manifest, its hash and every block digest are identical, so this is still v1. Before
the rebuild the evidence row read reward 0.966, gate 0.998, cost 8282, hard failures
INV-01 19, batch CAL-01 3. The reward shift mixes all three changes (the new EVD-03
weight moves every quality score a little); the gate and hard-failure change is the
engine fix.
```

Building v1 (three reference policies, 4 processes on a shared 4-core container)
took 400 s; `bench verify --jobs 4` regenerates and checks every case.

Reading the reference rows:

- Intervals are 95% normal-approximation intervals over cases (per metric, not
  simultaneous). Cost is the merchant's cost from the journal, USD minor units.
- `regret vs evidence` is the paired cost difference against the evidence policy on
  the same case and issuer draws (the counterfactual check CF-01 replays it).
- The oracle reads ground truth (a declared reference run) and is *more* expensive
  than the evidence policy: knowing a claim is invalid does not win it when the
  merchant lacks the documents, and fighting costs fees.
- Before the engine fix the honest reference failed a hard check on 19 of 10,000 cases
  (INV-01, ledger invariants); after it, on none. The oracle and always-accept
  policies still fail INV-01 on 155. On 3,000 cases from
  a throwaway salt (not v1), all 7 honest INV-01 failures had a refund in flight and
  ended with the cardholder over-credited at close: the check charges a violation
  that stands at close to the agent (F-8 in docs/VERIFIER_FINDINGS.md). A gate that
  demands zero hard failures over 10,000 cases would block the reference policy.

Difficulty tiers (`scruple/bench/spec.py`, our definition): `aligned`, the documents
point to the right answer; `misleading`, they point the wrong way (a valid claim with
every required kind held, or an invalid one missing some); `uninformative`, the
reason code only asks for documents every merchant holds.

### Scoring an agent

```
SCRUPLE_HELDOUT_SALT=... uv run scruple bench score --agent evidence --jobs 4
SCRUPLE_HELDOUT_SALT=... uv run scruple bench score --agent mypkg.agent:policy
SCRUPLE_HELDOUT_SALT=... uv run scruple bench score --agent "cli:python my_agent.py"
```

`module:callable` is a Python function that plays a `DisputeEpisode` through
`episode.call(tool, args)` (an optional `report` attribute gives the self-report).
`cli:<command>` speaks JSON lines (protocol in `scruple/bench/runner.py`) and sees only
tool results: no seed, no labels, no score. `--limit N` scores a prefix (rounded up to
a block, labelled as such). One real run, the evidence policy loaded as a module on
the first 1,000 cases:

```
scruple-dispute-bench v1 (1286baf3be84b4f9): scruple.agents.dispute_baselines:evidence_policy on 1,000 cases (prefix) in 15.7s; synthetic
| scruple.agents.dispute_baselines:evidence_policy | 1000 | 0.966 [0.961, 0.971] | 0.997 [0.994, 1.000] | 8268 [7624, 8912] | 0 [0, 0] | 0.782 [0.756, 0.808] | 0.441 [0.410, 0.472] | INV-01 3 |
```

Without the salt: `error: the benchmark is sealed: set SCRUPLE_HELDOUT_SALT to the
salt it was built with` (exit 2).

Limits: the cases come from our simulator (G-5, G-12: agent and verifier share one
world model); the salt is a process control (whoever holds it can enumerate the
cases); a CLI agent is isolated, a Python agent in the same process is not (G-10).

## Experiments

Each experiment is pre-registered (hypothesis, design, metric, pass bar, sample size,
analysis plan) and the pre-registration is committed before anything runs. Results
are added below it in a separate commit, with the raw JSON in `results/experiments/`.

- [E1 planted regressions](E1_planted_regressions.md): PASS, narrowly (7 of 7 flagged on
  the full benchmark; 0.84 of exercised folds; 3 of 60 control folds flagged).
- [E2 reward-hacked vs honest policy](E2_reward_hacking.md): FAIL on H2 (the
  proxy-optimised policy fabricates and overclaims, and is $33.87 per case *cheaper*
  on the simulated ledger; the scorecard catches it, the cost gate would promote it);
  the hacking-gap bounds hold and are tight.
