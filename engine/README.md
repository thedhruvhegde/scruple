# scruple engine

simulation and hack-resistant verifiers for agents that move money. card disputes first.

- the same verifier is three things: a reward for training, an eval for choosing a model, a gate for releasing a change.
- scores come from the ledger and a hash-chained trace. never from what the agent says it did.
- python 3.12, uv, pydantic v2, mypy strict. 5,193 tests.

## run it

```
make install
make demo        # e-value gate on 30k synthetic cases, about 15s
make test        # parallel, about 2.5 min
make lint typecheck
uv run scruple --help
```

## results

real data and real models are labelled real. everything else is synthetic, and says so.

**real models, real complaints** ([docs/results/REAL_MODELS.md](docs/results/REAL_MODELS.md))

- cfpb complaints, public domain, sealed test split. graded by the real label, never by a model.

| task | trained classifier | claude haiku | claude sonnet |
|---|---|---|---|
| reg e vs reg z, with a cited section | 0.87 | 0.90 | 0.93 |
| will the complaint end in relief (auroc) | 0.70 | 0.55 | 0.61 |
| which issue was filed | 0.58 | 0.56 | 0.54 |

- models know the rules. they judge outcomes worse than a simple classifier trained on real outcomes.

**real models, simulated disputes** ([docs/results/MODEL_EPISODES.md](docs/results/MODEL_EPISODES.md))

- haiku, sonnet and opus each worked 12 cases end to end through the cli. same seeds as three reference policies.
- sonnet cut merchant loss the most, $32.77 a case below the evidence policy [$4.33, $64.01]. 4 of its 10 wins were against cardholders who were right, one of them a fraud victim.
- haiku misstated its own cost or refunds in 4 of 12 cases.
- real models found 3 false positives in the self-report verifier. fixed, with tests.

**experiments, pre-registered before running** ([docs/experiments](docs/experiments))

- e1, planted regressions: pass, narrowly. all 7 planted regressions caught on the full benchmark. fold detection 0.844 against a 0.80 bar. false alarms 3 of 60, right at the 5% bar. a 1.3 cent a case regression slipped past the cost gate (first written as $1.30, a units slip). the paper shows a wider, still valid bet catches it.
- e2, reward hacking: fails its own h2. a policy tuned on win rate learned to fabricate documents and claim wins. it is $33.87 a case cheaper on the simulated ledger, so a cost-only gate would ship it. the hard checks flagged it on 20 of 20 folds. the hacking-gap bound held in all 1,800 checks.

**statistics** ([docs/math](docs/math))

- anytime-valid paired test. type i error at most 0.027 at alpha 0.05, across 4 null shapes and 7 stopping rules. peeking at a z-test gives 0.15 to 0.34.
- per case type e-bh. catches a regression in a 1% case type that pooled testing catches 0.6% of the time.
- sample-size planning. too little data is a verdict, "insufficient evidence", never a silent pass.
- a hacking-gap bound and the audit rate that makes hacking unprofitable. a candidate contribution, not yet checked against the literature.

**verifiers** ([docs/VERIFIER_FINDINGS.md](docs/VERIFIER_FINDINGS.md))

- 38 exploits from the research ([docs/research/VERIFIERS.md](docs/research/VERIFIERS.md)): 24 caught, 5 blocked, 1 partial, 1 not caught, 7 not expressible. the gaps are pinned in tests.
- mutation score on the verifiers: 64.7% of 2,173 mutants killed.

**simulator** ([docs/results/SIM_FIDELITY.md](docs/results/SIM_FIDELITY.md))

- visa and mastercard reason codes, the full lifecycle, deadlines, fees, fx, compelling evidence 3.0, monitoring programs. every rule cites a public source or a named assumption ([docs/RULES.md](docs/RULES.md)).
- 12.6k to 16.9k cases a second on one core. event-sourced, so any step replays or rewinds.
- calibrated against real cfpb marginals: total variation on fraud share drops from 0.152 to 0.010.
- frozen benchmark v1: 10,000 held-out cases behind a sealed salt. manifest `1286baf3…2cb13f91`.

## what's in it

```
scruple/sim          dispute engine, reason codes, lifecycle, scenarios, fidelity
scruple/verifiers    weighted checks, ledger invariants, exploits, traces
scruple/stats        e-processes, e-bh, planning, hacking-gap bounds
scruple/gate         shadow, canary, approvals, evidence packs
scruple/ci           github action, pr scorecard, gitlab template
scruple/agents       episodes, tools, anthropic and openai runners, baselines
scruple/mcp          mcp servers for the simulator and the gate
scruple/ingest       stripe, adyen, csv, bai2, camt.053, nacha, redaction, tokens
scruple/backtest     replay a partner's history, no look-ahead
scruple/realdata     cfpb loaders, frozen splits, graded tasks
scruple/bench        frozen held-out benchmark
plugins/claude-code  /scruple-gate and /scruple-episode
docs/pilot           offer, scope, success criteria, data checklist, security, demo script, terms draft
```

## a pilot

- [docs/pilot/README.md](docs/pilot/README.md): six weeks on a partner's own dispute history. import, backtest, shadow, gate in their ci, readout.
- [docs/pilot/DEMO_SCRIPT.md](docs/pilot/DEMO_SCRIPT.md): twenty minutes, every command real.
- [docs/pilot/SECURITY.md](docs/pilot/SECURITY.md): controls that exist, and the ones that don't yet.

## not yet

- no partner data has run through it. every real result above uses public data or real models on synthetic cases.
- authn trusts a header. no soc 2, no pen test.
- no hosted deployment. docker image and compose only.
- model runs used claude code subagents. the api runners are built but have no live key here.
