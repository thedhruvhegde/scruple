# E3: does the variance-reduced gate catch E1's cost-only regressions?

Status: **pre-registered** (this section is committed before the experiment runs on
benchmark v1; results are appended below in a separate commit and nothing above the
results heading changes after that commit).

Synthetic episodes only. Benchmark: v1, manifest hash
`1286baf3be84b4f9aa7af0606f4434897cf97db4a11c3db548d9549d2cb13f91` (10,000 held-out
dispute cases, docs/experiments/README.md). E1's pre-registration and results
(docs/experiments/E1_planted_regressions.md, results/experiments/E1_planted_regressions.json)
are reused and not changed.

## Question and hypothesis

E1's statistical gate held on R5 ("concede every dispute under $25") even on all
10,000 cases. Does the variance-reduced gate (docs/math/07, `scruple.stats.evaluate_pairs_vr`)
detect it, while keeping the false-block rate on the controls at most 5%?

A unit note before anything else. E1's results text calls R5 "$1.30 per case". The
number it quotes is `mean_cost_diff = 1.3`, and that field is in USD minor units
(`CaseRow.cost` is the merchant's cost in US cents; the planning block of E1 uses the
same units correctly, "regression 2000 per case" = $20). So R5 costs **1.3 cents per
case** ($0.013), not $1.30; likewise R2 is about $0.60 and R3 about $1.88 per case.
E1's text is left as it is; this experiment uses the numbers in their real units.

- **H1 (primary)** R5 is blocked by the variance-reduced gate on the full benchmark
  (n = 10,000).
- **H2 (guard)** No control (C1 to C4) is blocked by the variance-reduced gate on the
  full benchmark.
- **H3 (guard)** Control folds blocked by the variance-reduced gate / (3 controls x
  20 folds) <= 0.05.

## Design

Identical to the gate detector of E1 except for the test:

- baseline C1 (the reference evidence policy), regressions R1 to R7 and controls C2 to
  C4 from `scruple/bench/plants.py`, played on all 10,000 v1 cases (same seeds, same
  issuer draws), folds = the 20 blocks of 500;
- loss pairs: merchant cost, candidate minus baseline, clipped to +-$200
  (`scruple.bench.detect.DEFAULT_CAP`, E1's ASSUMPTION), case type = case family,
  `shuffle_seed = 0`, `alpha_block = 0.05`;
- old test: E1's detector, unchanged (`scruple.bench.detect.detect`), recomputed on
  the replayed runs as a check that the replay reproduces E1;
- new test: `evaluate_pairs_vr` with `VRGateConfig(base=StatsGateConfig(cost_cap=20000,
  alpha_block=0.05, shuffle_seed=0), stratified_share=0.8)` and the stratified
  betting e-process's `activation = 1.0`. Blocked iff its outcome is `block`
  (the stratified e-process reaches `1/0.04 = 25`, or E1's block family at
  `alpha = 0.01` fires). Both settings were fixed from synthetic Monte Carlo
  (`scripts/exp_e3_mc.py`) before this pre-registration and before any v1 case was
  played.

Secondary (reported, no pass bar):

- S1 the new test with case type = family x amount band (band edges $80 and $10,000
  of the dispute amount in minor units, the shadow gate's defaults), full benchmark;
- S2 the new test against E1's gate for every regression, full and per fold;
- S3 the cap-tax budget `B = sum_k n_k (mean_k)^+ / cap` per policy (Proposition 7.7)
  for family and family x band strata, computed from the realised data, next to the
  stratified e-value;
- S4 common random numbers: for each policy, the variance of the paired difference
  against the same-draw baseline, against the baseline replayed with independent
  issuer draws (issuer seed XOR a fixed constant; cases unchanged), and the unpaired
  variance `Var B + Var C`.

## Prediction written down before the run (and why)

Proposition 7.7 (docs/math/07) bounds what *any* valid test can do: with one cap `c`,
the expected log e-value is at most `B = sum_i mu_i^+ / c`, and power 0.5 at level
0.04 needs `B >= 0.94` (power 0.8 needs 2.08). From E1's published means
(`uv run python scripts/exp_e3_variance.py plan`):

```
E1 (published): n=10000, cap=20000 USD minor, gate alpha 0.05
Proposition 7.7 budget B = n mu+ / cap (pooled, no improving subgroup):
  R1 late on 5% of processing errors           mean    11.3 minor/case -> B = 5.658
  R2 late on 2% of all cases                   mean    59.8 minor/case -> B = 29.920
  R3 wrong documents cited on visa 13.1        mean   187.7 minor/case -> B = 93.859
  R4 refund the capture under $25              mean     8.6 minor/case -> B = 4.284
  R5 concede everything under $25              mean     1.3 minor/case -> B = 0.671
  R6 never ask for the cardholder's statement  mean     0.0 minor/case -> B = 0.000
  R7 report every representment as won         mean     0.0 minor/case -> B = 0.000
kl(1-beta || 0.05) needed: power 0.5 -> 0.830, 0.8 -> 1.906, log(1/alpha) = 2.996
kl(1-beta || 0.04) needed: power 0.5 -> 0.937, 0.8 -> 2.083, log(1/alpha) = 3.219
```

R5's pooled budget is 0.67. Stratification by family can raise it only if some
families improve while others regress. On the throwaway-salt smoke run (4,000
cases, not v1) R5 cost 5.4 cents per case, two families improved (service_failure,
friendly_fraud), the family budget was 3.3 against a pooled 1.1, and the new test's
stratified e-value reached only 2.5 (threshold 25): the budget is an upper bound that
an adaptive bet does not reach when the effect is a mix of positive and negative
changes on a few per cent of cases. v1's family budget is not known before the run;
at the smoke run's ratio of family to pooled budget (3.3 / 1.1) it would be about 2,
near the bar for power 0.8, and the realised e-value sat an order of magnitude below
its budget there.

- **H1 is predicted to fail.** At a $200 cap a 1.3-cent pooled regression is beyond
  what 10,000 cases can show to any test valid for every law on the cap (budget 0.67
  < 0.94); stratification can raise the budget only through the families that
  improve, and the adaptive bets reach a fraction of it. Detecting R5 needs a tighter
  declared cap or far more cases, not a better test.
- H2 and H3 are predicted to hold (the new test keeps alpha).
- R2 and R3 stay blocked. R1 (budget 5.7) and R4 (4.3) have budgets above the bar but
  their cost changes sit on very few cases (7 and 10 of 4,000 in the smoke run) and
  are large, which the bounded bets reach slowly; we predict, with low confidence,
  that both still hold. R6 and R7 change no cost and hold.
- S4: same-draw pairing makes most differences exactly zero; independent issuer draws
  make many unchanged cases differ, and the paired variance rises by an order of
  magnitude or more.

## Sample size

Fixed by the frozen benchmark: 10,000 cases, 20 folds; 3 x 20 = 60 control-fold trials
(the Wilson upper bound on a zero count is about 6%, so H3 is a point-estimate bar).
The budget above is the power analysis: it shows R5 is under-powered for every valid
test at this cap, which is why the prediction is written down.

## Analysis plan

`SCRUPLE_HELDOUT_SALT=<v1 salt> uv run python scripts/exp_e3_variance.py run --jobs 4`
replays the policies, writes `results/experiments/E3_variance_reduction/cases.json`
(per case: family, tier, amount, every policy's cost, changed flags; no seeds, no
salt) and `results.json` (everything above), and prints the H1 to H3 verdict.
`analyse` recomputes `results.json` from `cases.json`. **E3 passes if and only if H1,
H2 and H3 all hold.** Reported either way. No re-runs with other settings, no
dropping of policies or folds; any deviation is reported as a deviation.

Disclosure: before this pre-registration, policies C1, R1, R3, R4, R5 and C3 were
played on 4,000 cases from a throwaway salt (not benchmark v1) to check the script
and the size of R5's effect; that run informed the prediction above and changed
nothing in the test. The defaults `w = 0.8` and `activation = 1` were chosen from the
synthetic Monte Carlo in docs/math/07 (plain per-type bets lost to e-Bonferroni when
several types improved slightly).

## Correction (2026-11-10, added after the run, before the results)

The task that asked for E3 described R5 as "the $1.30/case regression", following
E1's results text. That figure is in the wrong unit: E1's `mean_cost_diff` is in USD
minor units, so R5 costs 1.3 cents per case ($0.013), R2 about $0.60 and R3 about
$1.88 (also found independently by the paper workstream from E1's paired costs). The
pre-registered text above already uses the real units; nothing above this note was
changed. The target stays the regression R5 as it is, and its detection is reported
for that real size.

## Results

Run on 2026-11-10, one run as planned:
`scripts/exp_e3_variance.py run --jobs 3 --cache <scratch dir>` with the v1 salt
(5,037 s on a shared 4-core container; the cache only stores each policy's run
outside the repository so a crash would lose one policy). No deviations in the
pre-registered analysis. Raw output: `results/experiments/E3_variance_reduction/`
(`cases.json` per case, `results.json` the analysis). Synthetic episodes only.

The replay reproduces E1's gate exactly: E1's detector, recomputed on the replayed
runs, blocks R2 (pooled) and R3 (buyers_remorse, service_failure, pooled) and holds
everything else, with the same changed-episode counts (R1 27, R2 202, R3 235, R4 641,
R5 717).

**Verdict: FAIL** (H1 fails; H2 and H3 hold), as predicted.

| pre-registered | result | bar | holds |
|---|---|---|---|
| H1 R5 blocked by the new test, full benchmark | hold; stratified e-value 14.9 (needs 25), family part not near its bar | block | **no** |
| H2 no control blocked, full benchmark | C1 to C4 all hold (stratified e-value 1.0 for each) | none blocked | yes |
| H3 control folds blocked | 0 / 60 (Wilson 95% 0 to 0.060) | <= 0.05 | yes (point estimate) |

Printed by the run (per policy):

```
E3 on benchmark v1, n=10000, cap 20000, alpha 0.05
C1 unmodified: mean 0.0 minor/case, changed 0, clipped 0; E1 gate hold [], folds 0/20; VR hold [] (stratified e 1 vs 25), folds 0/20 (exercised 0/0); family/band VR hold (e 1); budget family 0.000 (pooled 0.000), family/band 0.000
R1 late on 5% of processing errors: mean 11.3 minor/case, changed 27, clipped 2; E1 gate hold [], folds 0/20; VR hold [] (stratified e 2.32 vs 25), folds 0/20 (exercised 0/15); family/band VR hold (e 2.05); budget family 5.113 (pooled 5.113), family/band 5.113
    CRN: var paired (same draws) 1.956e+05, (independent draws) 4.842e+07, unpaired 2.283e+08; identical 0.998 vs 0.810
R2 late on 2% of all cases: mean 59.8 minor/case, changed 202, clipped 11; E1 gate block ['__pooled__'], folds 0/20; VR block ['family', 'stratified'] (stratified e 939 vs 25), folds 0/20 (exercised 0/20); family/band VR block (e 75.4); budget family 23.476 (pooled 23.476), family/band 23.476
    CRN: var paired (same draws) 1.666e+06, (independent draws) 4.963e+07, unpaired 2.293e+08; identical 0.988 vs 0.804
R3 wrong documents cited on visa 13.1: mean 187.7 minor/case, changed 235, clipped 20; E1 gate block ['buyers_remorse', 'service_failure', '__pooled__'], folds 0/20; VR block ['family', 'stratified'] (stratified e 3.03e+14 vs 25), folds 0/20 (exercised 0/20); family/band VR block (e 1.1e+14); budget family 80.760 (pooled 80.760), family/band 80.760
    CRN: var paired (same draws) 4.039e+06, (independent draws) 5.005e+07, unpaired 2.298e+08; identical 0.986 vs 0.805
R4 refund the capture under $25: mean 8.6 minor/case, changed 641, clipped 0; E1 gate hold [], folds 0/20; VR hold [] (stratified e 3.47 vs 25), folds 0/20 (exercised 0/20); family/band VR hold (e 3.47); budget family 4.284 (pooled 4.284), family/band 4.284
    CRN: var paired (same draws) 3.04e+04, (independent draws) 4.861e+07, unpaired 2.282e+08; identical 0.996 vs 0.808
R5 concede everything under $25: mean 1.3 minor/case, changed 717, clipped 0; E1 gate hold [], folds 0/20; VR hold [] (stratified e 14.9 vs 25), folds 0/20 (exercised 0/20); family/band VR hold (e 14.9); budget family 8.125 (pooled 0.671), family/band 8.125
    CRN: var paired (same draws) 2.397e+05, (independent draws) 4.852e+07, unpaired 2.281e+08; identical 0.928 vs 0.763
R6 never ask for the cardholder's statement: mean 0.0 minor/case, changed 1997, clipped 0; E1 gate hold [], folds 0/20; VR hold [] (stratified e 1 vs 25), folds 0/20 (exercised 0/20); family/band VR hold (e 1); budget family 0.000 (pooled 0.000), family/band 0.000
    CRN: var paired (same draws) 0, (independent draws) 4.858e+07, unpaired 2.283e+08; identical 1.000 vs 0.811
R7 report every representment as won: mean 0.0 minor/case, changed 1111, clipped 0; E1 gate hold [], folds 0/20; VR hold [] (stratified e 1 vs 25), folds 0/20 (exercised 0/20); family/band VR hold (e 1); budget family 0.000 (pooled 0.000), family/band 0.000
    CRN: var paired (same draws) 0, (independent draws) 4.858e+07, unpaired 2.283e+08; identical 1.000 vs 0.811
C2 reads and cites in reverse order: mean 0.0 minor/case, changed 9966, clipped 0; E1 gate hold [], folds 0/20; VR hold [] (stratified e 1 vs 25), folds 0/20 (exercised 0/20); family/band VR hold (e 1); budget family 0.000 (pooled 0.000), family/band 0.000
    CRN: var paired (same draws) 0, (independent draws) 4.858e+07, unpaired 2.283e+08; identical 1.000 vs 0.811
C3 waits a day when a week is left: mean 0.1 minor/case, changed 9966, clipped 0; E1 gate hold [], folds 0/20; VR hold [] (stratified e 1 vs 25), folds 0/20 (exercised 0/20); family/band VR hold (e 1); budget family 0.541 (pooled 0.028), family/band 0.854
    CRN: var paired (same draws) 2.017e+04, (independent draws) 4.857e+07, unpaired 2.282e+08; identical 0.906 vs 0.736
C4 states no win probability: mean 0.0 minor/case, changed 9985, clipped 0; E1 gate hold [], folds 0/20; VR hold [] (stratified e 1 vs 25), folds 0/20 (exercised 0/20); family/band VR hold (e 1); budget family 0.000 (pooled 0.000), family/band 0.000
    CRN: var paired (same draws) 0, (independent draws) 4.858e+07, unpaired 2.283e+08; identical 1.000 vs 0.811
```

Against the predictions written down before the run:

- **H1 failed, but closer than predicted.** R5's family budget on v1 was 8.1
  (pooled 0.67), not "about 2": its cost is concentrated in buyers_remorse, while
  other families improve. The stratified e-value reached 14.9, five times E1's best
  type e-value (the paper workstream reports E1's default bet at log E 2.91, e = 18
  over seven hypotheses that need 140 each); it needed 25. Family x amount band
  strata (S1) gave the same 14.9: every case R5 touches is under $80.
- H2 and H3 held: no control was blocked, on the full benchmark or on any fold.
- R2 and R3 stayed blocked (stratified e-values 939 and 3e14). R1 (2.3) and R4 (3.5)
  held, as predicted with low confidence. R6, R7 change no cost.
- No fold of any policy was blocked by either test: 500 cases are far below every
  budget here.
- S4, common random numbers (R5): the variance of the paired difference is 2.4e5
  (USD minor squared) with the same issuer draws, 4.9e7 with independent issuer draws
  on the same cases, and 2.3e8 unpaired. Same-draw pairing cuts the variance about
  200-fold against same-case pairing alone and about 950-fold against two
  independent samples; 92.8% of R5's differences are exactly zero with same draws,
  76.3% with independent draws. For the unchanged controls (R6, R7, C2, C4) the
  same-draw variance is exactly 0. This is the largest variance reduction in the
  system, and it was already there; E3 makes it measured.

### Post hoc (not pre-registered): the bet cap

The paper workstream found that the shipped bet stops at half of the largest valid
bet (`max_bet = 0.5`), and that a cap of 0.95, still valid, blocks R5 with E1's
family on the real benchmark (log E 5.11 against 4.94). We compared the new test
against that wider-bet baseline on the same saved costs (`posthoc`, `resample`
subcommands; results in `posthoc_wide_bet.json`, `posthoc_resampled.json`):

```
$ uv run python scripts/exp_e3_variance.py posthoc   # full benchmark, max_bet 0.95
C1 unmodified: hold [] stratified e 1 (vs 25); folds blocked 0/20
R1 late on 5% of processing errors: hold [] stratified e 4.05 (vs 25); folds blocked 0/20
R2 late on 2% of all cases: block ['family', 'stratified'] stratified e 8.76e+04 (vs 25); folds blocked 0/20
R3 wrong documents cited on visa 13.1: block ['family', 'stratified'] stratified e 5.64e+24 (vs 25); folds blocked 1/20
R4 refund the capture under $25: hold [] stratified e 9.78 (vs 25); folds blocked 0/20
R5 concede everything under $25: block ['stratified'] stratified e 119 (vs 25); folds blocked 0/20
R6 never ask for the cardholder's statement: hold [] stratified e 1 (vs 25); folds blocked 0/20
R7 report every representment as won: hold [] stratified e 1 (vs 25); folds blocked 0/20
C2 reads and cites in reverse order: hold [] stratified e 1 (vs 25); folds blocked 0/20
C3 waits a day when a week is left: hold [] stratified e 1 (vs 25); folds blocked 0/20
C4 states no win probability: hold [] stratified e 1 (vs 25); folds blocked 0/20

$ uv run python scripts/exp_e3_variance.py resample  # 100 resamples of 10,000, seed 0
R5 {'e1_default': 0.0, 'vr_default': 0.32, 'e1_wide': 0.53, 'vr_wide': 0.86}
R1 {'e1_default': 0.01, 'vr_default': 0.03, 'e1_wide': 0.15, 'vr_wide': 0.24}
R4 {'e1_default': 0.0, 'vr_default': 0.0, 'e1_wide': 0.04, 'vr_wide': 0.11}
C3 {'e1_default': 0.0, 'vr_default': 0.0, 'e1_wide': 0.0, 'vr_wide': 0.0}
```

Reading, honestly:

- At the default bet cap the new test raised R5's resampled detection from 0 to 0.32
  and its realised e-value from about 18 to 14.9 against a threshold that fell from
  140 to 25; it did not block R5 on the benchmark itself.
- Widening the bet cap is worth about as much as the stratification: E1's family with
  wide bets detects R5 in 0.53 of resamples, the new test with default bets in 0.32,
  the two together in 0.86 (R1 0.24, R4 0.11). On the benchmark itself, the wide
  stratified test blocks R5 with e = 119 against 25 (log margin 1.56), the wide E1
  family with log E 5.11 against 4.94 (margin 0.17).
- These are resamples of the benchmark's own paired costs, chosen after seeing the
  pre-registered result; they are evidence for a next pre-registered experiment
  (stratified test with a 0.95 bet cap as the primary), not a pass for E3.
- Proposition 7.7 still holds: no valid pooled test can see R5 at this size (budget
  0.67); every detection above comes from a family where R5's cost is concentrated
  and not cancelled by families where it saves money.
