# 1. anytime-valid paired test for loss regression

Label: **standard, applied** (test martingales, Ville, betting bets), with one small
**adaptation** (confidence sequence with mean-independent bets, giving an exact interval).

Code: `scruple/stats/sequential.py`. Tests: `tests/test_stats_sequential.py`.
Evidence: `scripts/math_sequential.py` (output below, synthetic, seeded).

## Setting

Each held-out or canary case `i` has a baseline loss `B_i` and a candidate loss `C_i`
(USD minor units, for example regret recomputed from the journal). The operator
declares, before seeing outcomes, a per-case cost cap `c` with `|C_i - B_i| <= c`. Write

    X_i = (C_i - B_i) / c  in [-1, 1]      (positive = candidate worse)

and `mu_i = E[X_i | F_{i-1}]`, where `F_{i-1}` is everything observed before case `i`.

Two one-sided composite nulls are used:

| use | null | rejecting means | type-I error is |
|---|---|---|---|
| canary monitoring (`alternative="greater"`) | `mu_i <= m` for all `i` | the candidate regressed beyond `m` | a false rollback |
| promotion certificate (`alternative="less"`) | `mu_i >= m` for all `i` | the candidate is better than `m` (non-inferior for `m > 0`) | promoting a harmful change |

The second row matters most: it is the error that costs money.

## Assumptions

- A1 (boundedness). `|C_i - B_i| <= c` for every case. The cap is a contract: a
  violation raises an error, it is never clipped (`normalise_differences`).
- A2 (null as conditional means). The null constrains `E[X_i | F_{i-1}]`. i.i.d. cases
  are a special case; nothing needs a variance bound, a CLT or independence.
- A3 (predictable bets). The bet for case `i` uses only `X_1..X_{i-1}`.

## Proposition 1.1 (betting e-process)

Let `s = +1` for "greater", `s = -1` for "less", `Y_i = s (X_i - m)`, and bets
`0 <= lambda_i <= b / (1 + s m)` with `b < 1`. Then `K_t = prod_{i<=t} (1 + lambda_i Y_i)`
satisfies, under the null,

    P( exists t >= 1 : K_t >= 1/alpha ) <= alpha,

and `E[K_tau] <= 1` for every stopping time `tau` (so `K_tau` is an e-value).

**Proof.** `Y_i >= -(1 + s m)` so each factor is at least `1 - b > 0`. Under the null,
`E[1 + lambda_i Y_i | F_{i-1}] = 1 + lambda_i s (mu_i - m) <= 1` because `lambda_i >= 0`
is `F_{i-1}`-measurable and `s (mu_i - m) <= 0`. So `K` is a non-negative
supermartingale with `K_0 = 1`. Ville's inequality (Ville 1939) gives the first claim;
optional stopping for non-negative supermartingales (Fatou) gives the second. QED.

This is the test-by-betting construction surveyed by Ramdas, Grünwald, Vovk and Shafer
(2023, "Game-theoretic statistics and safe anytime-valid inference", Statistical
Science) and used for bounded means by Waudby-Smith and Ramdas (2023, "Estimating
means of bounded random variables by betting", JRSS-B; arXiv 2010.09686).

**Bets.** Default `aGRAPA`: `lambda_i = clip(ybar / s2, 0, lambda_max)` with running
estimates of `E Y` and `E Y^2` (one pseudo-observation at 0 with second moment 1/4).
It is the maximiser of `lambda E Y - lambda^2 E Y^2 / 2`, the second-order expansion of
the growth rate `E log(1 + lambda Y)` (the Kelly criterion, Kelly 1956, "A new
interpretation of information rate"). The bet affects power only, never validity.

## Proposition 1.2 (exact-interval confidence sequence) (adaptation)

Let `lambda_i in [0, b]`, `b < 1/2`, be predictable and **not depend on the candidate mean
`m`**. Define `K+_t(m) = prod (1 + lambda_i (X_i - m))` and
`K-_t(m) = prod (1 + lambda_i (m - X_i))`. Then for i.i.d. data with mean `mu`,

    C_t = { m in [-1, 1] : K+_t(m) < 2/alpha and K-_t(m) < 2/alpha } = [L_t, U_t]

satisfies `P(forall t : mu in C_t) >= 1 - alpha`, and so does the running intersection.

**Proof.** For `m in [-1, 1]` and `b < 1/2`, every factor is at least `1 - 2b > 0`;
`K+_t(m)` is strictly decreasing and `K-_t(m)` strictly increasing in `m`, so the
non-rejected set is an interval and its end points are found by bisection (rounded
outwards). At `m = mu` both are test martingales (Proposition 1.1 with equality), so by
Ville and a union bound over two sides at `alpha/2` each the true mean is never
excluded with probability at least `1 - alpha`. On that event every `C_t` contains `mu`,
hence so does any intersection. QED.

Bets are the predictable plug-in of Waudby-Smith and Ramdas,
`lambda_t = min(b, sqrt(2 log(2/alpha) / (sigma2_{t-1} t log(1 + t))))`. Their
hedged-capital confidence sequence uses `m`-dependent bets and is tighter; our
`m`-independent choice is the adaptation, bought for an exact interval with no grid.
`bet_alpha` decouples bet tuning from the reported level, which makes the intervals
nested in `alpha` (tested).

## What is known vs new

Everything in 1.1 is standard. 1.2 is a minor variant of known betting confidence
sequences; we claim nothing novel here. Using these for paired agent-change
comparisons in canary is an application. Closest applied work we found:
Lindon et al. (2022, "Rapid Regression Detection in Software Deployments through
Sequential Testing", arXiv 2205.14762) for sequential canary tests. A literature check
is still required.

## Simulation evidence (synthetic, seeded)

`uv run python scripts/math_sequential.py`. Nulls at the boundary `mu = m = 0`;
seven stopping rules applied to the **same** paths, including data-dependent ones
("stop when it looks bad", "give up when wealth < 1/2", "peek every 50"); `ever_crossed`
is the supremum over the horizon, the worst stopping rule. The naive comparison is a
one-sided z-test re-run every 10 cases.

```
## type-I error at the null boundary (alpha=0.05, reps=2000, horizon=2000)
3 x binomial SE at alpha: 0.0146
ternary_mostly_zero: ever_crossed=0.0265, first_crossing=0.0265, fixed_horizon=0.0020, give_up_below_half=0.0215, peek_every_50=0.0180, random_time=0.0005, stop_when_looks_bad=0.0000; naive_z_peek_every_10=0.3255
continuous_beta: ever_crossed=0.0270, first_crossing=0.0270, fixed_horizon=0.0020, give_up_below_half=0.0200, peek_every_50=0.0120, random_time=0.0015, stop_when_looks_bad=0.0000; naive_z_peek_every_10=0.3430
rare_large_loss: ever_crossed=0.0090, first_crossing=0.0090, fixed_horizon=0.0020, give_up_below_half=0.0090, peek_every_50=0.0070, random_time=0.0005, stop_when_looks_bad=0.0000; naive_z_peek_every_10=0.1470
always_changes: ever_crossed=0.0265, first_crossing=0.0265, fixed_horizon=0.0000, give_up_below_half=0.0185, peek_every_50=0.0050, random_time=0.0000, stop_when_looks_bad=0.0075; naive_z_peek_every_10=0.3385
worst betting rejection rate over all nulls and rules: 0.0270

## power: P(reject by n=3000), ternary p_changed=0.1 |x|=0.8, reps=1000
mu=0.0000: power=0.020, median stopping n=324
mu=0.0025: power=0.045, median stopping n=470
mu=0.0050: power=0.087, median stopping n=736
mu=0.0100: power=0.313, median stopping n=1338
mu=0.0150: power=0.720, median stopping n=1434
mu=0.0200: power=0.935, median stopping n=1000
mu=0.0300: power=1.000, median stopping n=446
mu=0.0400: power=1.000, median stopping n=261

## confidence sequence (alpha=0.1), mu=0.01, reps=500
simultaneous coverage at checkpoints (25, 100, 500, 1000, 2000): 0.964
mean width: n=25: 0.6029, n=100: 0.1826, n=500: 0.0768, n=1000: 0.0561, n=2000: 0.0417
```

Reading: the worst betting rejection rate over all null shapes and stopping rules is
far below `alpha = 0.05` (the time-uniform bound is conservative at a finite horizon),
while peeking at a z-test inflates the false-rollback rate to roughly 0.15 to 0.34.
Power reaches 0.9 at a mean regression of 0.02 cap units per case by 3000 cases.
The confidence sequence covers at every checkpoint simultaneously at least `1 - alpha`.

## Limitations

- The cap tax: power depends on `mu / c`. A loose cap makes regressions look tiny.
  No valid nonparametric test escapes this (see 03, Proposition 3.3).
- A2 is about conditional means. Drift that makes later cases worse is detected as a
  regression of those cases, which is the desired behaviour, but the e-process answers
  "is the conditional mean ever above `m`", not "is the average above `m`".
- Delayed outcomes (dispute outcomes resolve in weeks) are out of scope: the test
  consumes a case when its loss is known.

## References

- J. Ville (1939). Étude critique de la notion de collectif. Gauthier-Villars.
- J. L. Kelly (1956). A new interpretation of information rate. Bell System Technical Journal.
- S. R. Howard, A. Ramdas, J. McAuliffe, J. Sekhon (2021). Time-uniform, nonparametric,
  nonasymptotic confidence sequences. Annals of Statistics 49(2).
- A. Ramdas, P. Grünwald, V. Vovk, G. Shafer (2023). Game-theoretic statistics and safe
  anytime-valid inference. Statistical Science.
- I. Waudby-Smith, A. Ramdas (2023). Estimating means of bounded random variables by
  betting. JRSS-B (read before the RSS in 2023; arXiv 2010.09686).
- R. Johari, P. Koomen, L. Pekelis, D. Walsh (2017). Peeking at A/B tests: why it
  matters, and what to do about it. KDD.
- M. Lindon et al. (2022). Rapid regression detection in software deployments through
  sequential testing. arXiv 2205.14762.
