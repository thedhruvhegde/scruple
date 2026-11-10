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

## Results
