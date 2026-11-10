# docs/math: statistics for deciding whether an agent change is safe to promote

This folder is the write-up behind `scruple/stats/`. Each file states a setting,
assumptions, results with proofs (or proof sketches citing the standard result), what is
known versus new, and simulation evidence pasted from a seeded script in `scripts/`.
**All data is synthetic.** Every illustrative cost, cap and prior is an ASSUMPTION.

## Results

| # | file | result | label |
|---|---|---|---|
| 1 | [01_anytime_paired_test.md](01_anytime_paired_test.md) | betting e-process for the mean paired loss difference; exact-interval confidence sequence; valid under continuous monitoring and any stopping rule | standard, applied (CS variant: adaptation) |
| 2 | [02_stratified_guarantees.md](02_stratified_guarantees.md) | per-case-type e-processes on one stream; time-uniform weighted e-Bonferroni, closed mean testing (FWER), weighted e-BH (FDR); pooled tests miss rare-type regressions | standard, applied; adaptation |
| 3 | [03_sample_size.md](03_sample_size.md) | sufficient sample size (growth bound plus Bernstein, exact binomial counts, optimised alpha allocation); a KL lower bound showing every valid test pays for a loose cap | adaptation; standard |
| 4 | [04_asymmetric_decision.md](04_asymmetric_decision.md) | Bayes promote/block threshold under asymmetric costs; composition with the certificate keeps the frequentist guarantee and is Bayes-optimal among certified rules | standard; adaptation |
| 5 | [05_hacking_gap.md](05_hacking_gap.md) | hacking gap (proxy minus ledger-anchored mean); tail and expected bounds for accepted changes under spot-check audits; minimal audit rate; free resubmission defeats spot checks; 5.5-5.8: adaptive agents with leaky audits (supermartingale proof), minimal audit rate with verifier false negatives and false alarms | candidate novel contribution (framing), literature check required; 5.5-5.6 adaptation |
| 7 | [07_variance_aware_betting.md](07_variance_aware_betting.md) | variance-adaptive bets (grid mixture within log J of the best constant bet; WSR plug-in; wider aGRAPA cap); the best constant-bet growth is a KL distance to the null, giving a sample-size floor from observed costs; re-analysis of E1 | adaptation |
| 8 | [08_composition.md](08_composition.md) | composition: the promote decision carries the smallest level among conjuncts covering a harm (intersection-union), false blocks add up, alpha spending for resubmission, per-type certificates; what is not covered | standard, applied; adaptation |
| 9 | [09_variance_reduction.md](09_variance_reduction.md) | variance reduction that keeps anytime validity: stratified betting on one wealth process, control variates with a predictable coefficient, centred inverse-propensity canary terms, planning, the cap tax survives | adaptation; standard, applied |
| 6 | [06_gate_adapter.md](06_gate_adapter.md) | pure adapter `evaluate_pairs` and how to wire it into the gate; results on the demo's held-out set | engineering |

"Candidate novel" means we did not find it stated this way in a short search. It is not
a claim of novelty: a proper literature review is required before any paper says so.

## Headline numbers (synthetic, seeded)

- Type-I error of the betting test at the null boundary, worst over 4 null shapes and
  7 stopping rules (2,000 paths of 2,000 cases): **0.027** at `alpha = 0.05`. Peeking at
  a z-test every 10 cases: **0.15 to 0.34**.
- Rare type (1% of traffic) regresses by 0.35 cap units while the average improves:
  pooled anytime test flags it **0.6%** of the time, stratified e-Bonferroni **100%**.
- Zero-tolerance slice check (the current gate) at the null boundary: false-blocks
  **73% to 100%** of the time; stratified FWER procedures: **at most 3%** (`alpha = 0.1`).
- Sample size for a $5 regression, cap $100, sd $20, 1% stratum: valid plan 795 stratum
  cases (simulated minimum about 341); optimised alpha allocation saves **28% to 35%**
  of total traffic.
- Bayes rule alone promotes **61%** of boundary-harmful changes at 1:1 costs; composed
  with the certificate, **0.2%**.
- Hacking: best adversarial policy's expected accepted gap 0.0323 vs bound **0.0359**
  (`n = 200, q = 0.05`); a learning optimiser stops hacking exactly at `q* = g/(V+C+g)`.
- Leaky audits (05, 5.5-5.6): on the 120 E2 batches the leak-adjusted bound is attained
  (ratio 1.000), the naive bound is broken (0.251 vs 0.052 at `q = 0.05`, `beta = 0.3`,
  likelihood-ratio floor 0.2). With verifier errors `q* = g/((1-beta)(K+g) - phi K)`.
- Betting (07): on E1's paired costs the default bet's cap binds; a wider valid bet lifts
  R2's resampled power at 5,000 cases from 0.37 to 0.91 and blocks R5 (about one cent
  per case pooled, $0.64 per case in buyer's remorse) on the benchmark itself.
- Composition (08): a pooled certificate promotes a rare-type regression in 64% of runs
  at 20,000 cases; per-type certificates in 0%. Twenty resubmissions at fixed alpha
  0.05: 40% chance one harmful version ships; with spending, 1.7%.
- On the demo's 3,000 held-out cases the adapter **holds** both the good and the bad
  rule: with caps declared from amounts, no valid nonparametric test has the power
  (at least about 3,500 late-evidence cases needed; the demo has 178).

## Reproduce

```bash
uv run python scripts/math_sequential.py   # about 2 min
uv run python scripts/math_stratified.py   # about 2 min
uv run python scripts/math_planning.py     # under 1 min
uv run python scripts/math_decision.py     # about 1 min
uv run python scripts/math_hacking.py      # under 1 min
uv run python scripts/math_hacking_general.py   # seconds
uv run python scripts/math_gate_demo.py    # seconds
uv run python scripts/math_composition.py  # a few minutes
uv run python scripts/math_betting.py analyse   # about 10 minutes
make test        # fast property and Monte Carlo tests
make test-slow   # long seeded Monte Carlo runs (@pytest.mark.slow)
```

Outputs are deterministic (every random source takes an explicit seed). The blocks in
each file are pasted verbatim from these scripts.

## Notation

`B_i, C_i` baseline and candidate loss; `c` cost cap; `X_i = (C_i - B_i)/c in [-1, 1]`;
`m` null mean (tolerance over cap); `lambda` bet; `K_t` wealth (e-process); `alpha`
level; `beta` type-II error; `pi_k` type frequency; `q` audit rate; `d` detection
probability; `Gamma` hacking gap.

## What this does not cover

Delayed outcomes in canary (weeks of chargeback lag), two-sample canary designs,
distribution shift between held-out and live traffic (gap G-2), and the independence of
the verifier from the simulator (gap G-4). These are listed as open in each file.
