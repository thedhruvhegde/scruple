# 7. variance-aware bets, and a floor on what any valid test can see

Label: **adaptation** (7.1: mixture of constant bets, standard pieces; 7.2: the
`K_inf` duality of Honda and Takemura 2010, with a self-contained proof of the
direction we use) and **standard, applied** (the predictable plug-in bet of
Waudby-Smith and Ramdas 2023).

Code: `scruple/stats/betting.py`. Tests: `tests/test_stats_betting.py`.
Evidence: `scripts/math_betting.py` on the E1 paired costs
(`results/experiments/E1_paired_costs.json`).

A separate workstream is adding control variates to `scruple/stats/variance.py`;
nothing here depends on it, and the two compose (a control-variate-adjusted
difference is still a bounded observation if its cap is declared).

## Why the default bet leaves power on the table

Paired cost differences are mostly zero with a few large values. The default bet
(aGRAPA, `lambda = clip(ybar / s2, 0, 0.5/(1 + s m))`) has two weaknesses there: the cap at
half the largest valid bet binds whenever the variance is small relative to the
mean, and the plug-in `ybar` is noisy for a long time when most observations are
zero. Every strategy below is valid for the same null; they differ only in power.

| strategy | bet | note |
|---|---|---|
| `agrapa` | `clip(ybar/s2, 0, 0.5/(1+sm))` | the shipped default |
| `agrapa_wide` | same, cap `0.95/(1+sm)` | |
| `plugin` | `min(0.95/(1+sm), sqrt(2 log(1/a) / (s2_{t-1} t log(1+t))))` | WSR predictable plug-in; uses the variance, never the mean |
| `mixture` | average of 14 constant bets `0.95/(1+sm) 2^-j` | Proposition 7.1 |

## Proposition 7.1 (grid mixture)

Let `lambda_j = lambda_max rho^j`, `j = 0..J-1`, with `lambda_max <= b/(1 + s m)`, `b < 1`.
Then `E_t = (1/J) sum_j prod_{i<=t} (1 + lambda_j Y_i)` is a test supermartingale under
`H0`, and on every path

    log E_t >= max_j log K_t(lambda_j) - log J.

If the `Y_i` are i.i.d. and `g(lambda) = E log(1 + lambda Y)` has its maximiser
`lambda*` in `[lambda_{J-1}, lambda_0]`, then `liminf_t (1/t) log E_t >= rho g(lambda*)`
almost surely.

**Proof.** Each `K_t(lambda_j)` is a test supermartingale (01, Proposition 1.1, with a
constant bet); a convex combination of non-negative supermartingales with the same
filtration is one, with `E_0 = 1`. The sum is at least any one of its terms, which is
the path bound. For the rate, the strong law gives `(1/t) log K_t(lambda_j) -> g(lambda_j)`
for each of the finitely many `j`, so `liminf (1/t) log E_t >= max_j g(lambda_j)`. Some
`lambda_j` lies in `[rho lambda*, lambda*]`; `g` is concave with `g(0) = 0`, so
`g(lambda_j) >= (lambda_j / lambda*) g(lambda*) >= rho g(lambda*)`. QED.

With `rho = 1/2` and `J = 14` the mixture covers bets from `0.95` down to about `1e-4`
of the largest valid bet, at a cost of `log 14 = 2.64` nats of wealth, compared with a
threshold of `log(7/0.05) = 4.94` nats in the E1 gate. The rate guarantee is against
the best *constant* bet (the Kelly bet for the true law; Kelly 1956).

## Proposition 7.2 (the floor: the best constant-bet growth is a KL distance to the null)

Let `P` be a law on `[-1, 1]` with mean above `m`, `Y = X - m`, `lbar = 1/(1 + m)` and
`g(lambda) = E_P log(1 + lambda Y)` on the closed interval `[0, lbar]`, maximised at
`lambda*` with `g* > 0`. Suppose either (i) `lambda* < lbar`, or (ii) `lambda* = lbar` and
`P` puts no mass near `-1` (`P([-1, -1 + e0)) = 0` for some `e0 > 0`; true for any
empirical law without an atom at `-1`). Let

    Q = P / (1 + lambda* Y) + w delta_{-1},   w = 1 - E_P[1/(1 + lambda* Y)].

Then `w >= 0` (`w = 0` in case (i)), `Q` is a probability with mean exactly `m`, and
`KL(P || Q) = g*`. So every level-`alpha` test valid for all laws on `[-1, 1]` with mean
`<= m` and with power `1 - beta` against `P` needs `n >= kl(1 - beta || alpha) / g*`
i.i.d. observations (and `E_P[tau]` obeys the same bound for sequential tests).

**Proof.** Write `c(lambda) = E_P[Y/(1 + lambda Y)]`, the derivative of `g` where the
integrand is bounded (on a neighbourhood of an interior `lambda*`, and on all of
`[0, lbar]` in case (ii), because `1 + lbar Y = (1 + X)/(1 + m) >= e0/(1 + m)`). Concavity
gives `c(lambda*) = 0` in case (i) and `c(lbar) >= 0` in case (ii). Because
`1/(1 + lambda Y) = 1 - lambda Y/(1 + lambda Y)`, `w = lambda* c(lambda*) >= 0`, and `Q` has
mass 1 and a positive density on the support of `P`. Its mean shift is
`E_Q[Y] = c(lambda*) - w (1 + m) = c(lambda*)(1 - lambda* (1 + m)) = 0` in both cases, so
`Q` is in the null. `P` does not charge `-1` where `w > 0`, so `dP/dQ = 1 + lambda* Y` on
the support of `P` and `KL(P || Q) = g*`. Data processing on the rejection event
(03, Proposition 3.3) gives the bound; the sequential version is Kaufmann, Cappé and
Garivier (2016, Lemma 1). QED.

The converse (`KL(P || Q') >= g*` for every null `Q'`, so `g*` is exactly the smallest
divergence) follows from the Donsker-Varadhan inequality with the test function
`log(1 + lambda* Y)`; together they are the duality of Honda and Takemura (2010). We use
only the direction proved above. Applied to an empirical law, the floor answers "if
cases were drawn i.i.d. from the paired costs we observed, how many would any valid
test need?". It describes the observed distribution, not a population.

## Evidence on the E1 planted regressions (synthetic)

`uv run python scripts/math_betting.py analyse` (reads
`results/experiments/E1_paired_costs.json`, written by
`SCRUPLE_HELDOUT_SALT=... uv run python scripts/math_betting.py collect --jobs 4`, which
replays the E1 policies on all 10,000 benchmark-v1 cases and stores only the paired
merchant-cost differences, USD minor units). The gate is the E1 detector's block family
(six families plus pooled, weighted e-Bonferroni on running maxima, cap $200, alpha
0.05, shuffle seed 0) with each bet in turn.

```
## E1 paired merchant costs, benchmark v1, n=10000, cap 20000 USD minor (synthetic)
strategies: agrapa, agrapa_wide, plugin, mixture; alpha 0.05, 6 families + pooled

C2 reads and cites in reverse order: mean diff 0.00 USD minor/case (sd 0.0, t 0.00)
    agrapa     full: hold; folds with a gate block 0/20
    agrapa_wide full: hold; folds with a gate block 0/20
    plugin     full: hold; folds with a gate block 0/20
    mixture    full: hold; folds with a gate block 0/20
C3 waits a day when a week is left: mean diff 0.06 USD minor/case (sd 142.0, t 0.04)
    agrapa     full: hold; folds with a gate block 0/20
    agrapa_wide full: hold; folds with a gate block 0/20
    plugin     full: hold; folds with a gate block 0/20
    mixture    full: hold; folds with a gate block 0/20
C4 states no win probability: mean diff 0.00 USD minor/case (sd 0.0, t 0.00)
    agrapa     full: hold; folds with a gate block 0/20
    agrapa_wide full: hold; folds with a gate block 0/20
    plugin     full: hold; folds with a gate block 0/20
    mixture    full: hold; folds with a gate block 0/20
R1 late on 5% of processing errors: mean diff 11.32 USD minor/case (sd 442.3, t 2.56)
    agrapa     full: hold; folds with a gate block 0/20
    agrapa_wide full: hold; folds with a gate block 0/20
    plugin     full: hold; folds with a gate block 0/20
    mixture    full: hold; folds with a gate block 0/20
R2 late on 2% of all cases: mean diff 59.84 USD minor/case (sd 1290.9, t 4.64)
    agrapa     full: ['__pooled__']; folds with a gate block 0/20
    agrapa_wide full: ['friendly_fraud', '__pooled__']; folds with a gate block 0/20
    plugin     full: ['__pooled__']; folds with a gate block 0/20
    mixture    full: ['__pooled__']; folds with a gate block 0/20
R3 wrong documents cited on visa 13.1: mean diff 187.72 USD minor/case (sd 2009.9, t 9.34)
    agrapa     full: ['buyers_remorse', 'service_failure', '__pooled__']; folds with a gate block 0/20
    agrapa_wide full: ['buyers_remorse', 'service_failure', '__pooled__']; folds with a gate block 0/20
    plugin     full: ['buyers_remorse', 'service_failure', '__pooled__']; folds with a gate block 0/20
    mixture    full: ['buyers_remorse', 'service_failure', '__pooled__']; folds with a gate block 0/20
R4 refund the capture under $25: mean diff 8.57 USD minor/case (sd 174.4, t 4.91)
    agrapa     full: hold; folds with a gate block 0/20
    agrapa_wide full: hold; folds with a gate block 0/20
    plugin     full: hold; folds with a gate block 0/20
    mixture    full: hold; folds with a gate block 0/20
R5 concede everything under $25: mean diff 1.34 USD minor/case (sd 489.6, t 0.27)
    agrapa     full: hold; folds with a gate block 0/20
    agrapa_wide full: ['buyers_remorse']; folds with a gate block 0/20
    plugin     full: hold; folds with a gate block 0/20
    mixture    full: hold; folds with a gate block 0/20
R6 never ask for the cardholder's statement: mean diff 0.00 USD minor/case (sd 0.0, t 0.00)
    agrapa     full: hold; folds with a gate block 0/20
    agrapa_wide full: hold; folds with a gate block 0/20
    plugin     full: hold; folds with a gate block 0/20
    mixture    full: hold; folds with a gate block 0/20
R7 report every representment as won: mean diff 0.00 USD minor/case (sd 0.0, t 0.00)
    agrapa     full: hold; folds with a gate block 0/20
    agrapa_wide full: hold; folds with a gate block 0/20
    plugin     full: hold; folds with a gate block 0/20
    mixture    full: hold; folds with a gate block 0/20

## best gate e-value (max over the 6 families and pooled, running max), log scale
rejection needs log E >= log(7/0.05) = 4.94
C3 waits a day when a week is left: agrapa 0.16, agrapa_wide 0.24, plugin 0.39, mixture 0.06
R1 late on 5% of processing errors: agrapa 2.13, agrapa_wide 3.54, plugin 2.84, mixture 1.54
R2 late on 2% of all cases: agrapa 9.91, agrapa_wide 16.62, plugin 7.93, mixture 14.23
R3 wrong documents cited on visa 13.1: agrapa 34.42, agrapa_wide 58.78, plugin 15.84, mixture 56.71
R4 refund the capture under $25: agrapa 1.96, agrapa_wide 3.56, plugin 3.66, mixture 1.54
R5 concede everything under $25: agrapa 2.91, agrapa_wide 5.11, plugin 4.62, mixture 2.80

## power by i.i.d. resampling of cases (family kept with its diff)
P(gate blocks) over 100 resamples per cell, seed 7
R1 late on 5% of processing errors n=  2500: agrapa 0.00, agrapa_wide 0.00, plugin 0.00, mixture 0.00
R1 late on 5% of processing errors n=  5000: agrapa 0.00, agrapa_wide 0.00, plugin 0.00, mixture 0.00
R1 late on 5% of processing errors n= 10000: agrapa 0.00, agrapa_wide 0.17, plugin 0.00, mixture 0.01
R2 late on 2% of all cases n=  2500: agrapa 0.00, agrapa_wide 0.19, plugin 0.00, mixture 0.00
R2 late on 2% of all cases n=  5000: agrapa 0.37, agrapa_wide 0.91, plugin 0.43, mixture 0.64
R2 late on 2% of all cases n= 10000: agrapa 1.00, agrapa_wide 1.00, plugin 0.99, mixture 1.00
R3 wrong documents cited on visa 13.1 n=  2500: agrapa 1.00, agrapa_wide 1.00, plugin 1.00, mixture 1.00
R3 wrong documents cited on visa 13.1 n=  5000: agrapa 1.00, agrapa_wide 1.00, plugin 1.00, mixture 1.00
R3 wrong documents cited on visa 13.1 n= 10000: agrapa 1.00, agrapa_wide 1.00, plugin 1.00, mixture 1.00
R4 refund the capture under $25 n=  2500: agrapa 0.00, agrapa_wide 0.00, plugin 0.00, mixture 0.00
R4 refund the capture under $25 n=  5000: agrapa 0.00, agrapa_wide 0.00, plugin 0.00, mixture 0.00
R4 refund the capture under $25 n= 10000: agrapa 0.00, agrapa_wide 0.04, plugin 0.01, mixture 0.00
R5 concede everything under $25 n=  2500: agrapa 0.00, agrapa_wide 0.00, plugin 0.00, mixture 0.00
R5 concede everything under $25 n=  5000: agrapa 0.00, agrapa_wide 0.01, plugin 0.02, mixture 0.00
R5 concede everything under $25 n= 10000: agrapa 0.00, agrapa_wide 0.43, plugin 0.39, mixture 0.02

## the floor for any valid test (Prop 7.2) on the observed paired costs
g* = max_lambda mean log(1 + lambda x), x = clip(diff / cap); per family and pooled
n_k: cases of that family any valid level-alpha test needs for power 0.8;
total = n_k / share of the family
R1 late on 5% of processing errors:
    __pooled__           mean    10.23 g*=3.883e-04 lambda*=1.000: n_k >= 4,910 (alpha 0.05), 8,897 (alpha 0.05/7); total >= 4,910 / 8,897
    buyers_remorse       mean     0.00: no positive growth (mean not above 0)
    cardholder_mistake   mean   148.73 g*=5.447e-03 lambda*=1.000: n_k >= 350 (alpha 0.05), 634 (alpha 0.05/7); total >= 6,065 / 10,990
    friendly_fraud       mean     0.00: no positive growth (mean not above 0)
    merchant_error       mean    26.31 g*=1.183e-03 lambda*=1.000: n_k >= 1,611 (alpha 0.05), 2,920 (alpha 0.05/7); total >= 25,782 / 46,715
    service_failure      mean     0.00: no positive growth (mean not above 0)
    true_fraud           mean     0.00: no positive growth (mean not above 0)
R2 late on 2% of all cases:
    __pooled__           mean    46.95 g*=1.741e-03 lambda*=1.000: n_k >= 1,095 (alpha 0.05), 1,984 (alpha 0.05/7); total >= 1,095 / 1,984
    buyers_remorse       mean    67.05 g*=2.391e-03 lambda*=1.000: n_k >= 797 (alpha 0.05), 1,445 (alpha 0.05/7); total >= 4,033 / 7,308
    cardholder_mistake   mean    78.74 g*=2.890e-03 lambda*=1.000: n_k >= 660 (alpha 0.05), 1,195 (alpha 0.05/7); total >= 11,431 / 20,713
    friendly_fraud       mean    61.20 g*=2.292e-03 lambda*=1.000: n_k >= 832 (alpha 0.05), 1,507 (alpha 0.05/7); total >= 2,980 / 5,399
    merchant_error       mean    88.77 g*=3.364e-03 lambda*=1.000: n_k >= 567 (alpha 0.05), 1,027 (alpha 0.05/7); total >= 9,068 / 16,430
    service_failure      mean    15.72 g*=5.924e-04 lambda*=1.000: n_k >= 3,218 (alpha 0.05), 5,831 (alpha 0.05/7); total >= 17,490 / 31,692
    true_fraud           mean    16.57 g*=6.513e-04 lambda*=1.000: n_k >= 2,927 (alpha 0.05), 5,304 (alpha 0.05/7); total >= 13,366 / 24,218
R3 wrong documents cited on visa 13.1:
    __pooled__           mean   161.52 g*=6.181e-03 lambda*=1.000: n_k >= 308 (alpha 0.05), 559 (alpha 0.05/7); total >= 308 / 559
    buyers_remorse       mean   693.40 g*=2.634e-02 lambda*=1.000: n_k >= 72 (alpha 0.05), 131 (alpha 0.05/7); total >= 366 / 663
    cardholder_mistake   mean     0.00: no positive growth (mean not above 0)
    friendly_fraud       mean     0.00: no positive growth (mean not above 0)
    merchant_error       mean     0.00: no positive growth (mean not above 0)
    service_failure      mean   132.80 g*=5.291e-03 lambda*=1.000: n_k >= 360 (alpha 0.05), 653 (alpha 0.05/7); total >= 1,958 / 3,548
    true_fraud           mean     0.00: no positive growth (mean not above 0)
R4 refund the capture under $25:
    __pooled__           mean     8.57 g*=3.951e-04 lambda*=1.000: n_k >= 4,825 (alpha 0.05), 8,742 (alpha 0.05/7); total >= 4,825 / 8,742
    buyers_remorse       mean     0.00: no positive growth (mean not above 0)
    cardholder_mistake   mean     0.00: no positive growth (mean not above 0)
    friendly_fraud       mean     0.13 g*=6.656e-06 lambda*=1.000: n_k >= 286,432 (alpha 0.05), 518,996 (alpha 0.05/7); total >= 1,026,272 / 1,859,536
    merchant_error       mean     6.09 g*=2.874e-04 lambda*=1.000: n_k >= 6,632 (alpha 0.05), 12,017 (alpha 0.05/7); total >= 106,116 / 192,275
    service_failure      mean    19.04 g*=8.884e-04 lambda*=1.000: n_k >= 2,146 (alpha 0.05), 3,888 (alpha 0.05/7); total >= 11,662 / 21,131
    true_fraud           mean    21.21 g*=9.673e-04 lambda*=1.000: n_k >= 1,971 (alpha 0.05), 3,571 (alpha 0.05/7); total >= 8,999 / 16,306
R5 concede everything under $25:
    __pooled__           mean     1.34 g*=3.744e-06 lambda*=0.112: n_k >= 509,153 (alpha 0.05), 922,550 (alpha 0.05/7); total >= 509,153 / 922,550
    buyers_remorse       mean    64.35 g*=2.766e-03 lambda*=1.000: n_k >= 689 (alpha 0.05), 1,249 (alpha 0.05/7); total >= 3,486 / 6,316
    cardholder_mistake   mean    -8.15: no positive growth (mean not above 0)
    friendly_fraud       mean   -29.88: no positive growth (mean not above 0)
    merchant_error       mean    26.80 g*=9.454e-04 lambda*=1.000: n_k >= 2,016 (alpha 0.05), 3,654 (alpha 0.05/7); total >= 32,263 / 58,459
    service_failure      mean   -33.14: no positive growth (mean not above 0)
    true_fraud           mean     8.46 g*=3.847e-04 lambda*=1.000: n_k >= 4,956 (alpha 0.05), 8,980 (alpha 0.05/7); total >= 22,630 / 41,005

## validity at the null boundary: i.i.d. resamples of centred diffs, one e-process
    R5 agrapa      P(reject by n=2000) = 0.0000 (alpha 0.05, 3 SE 0.0327)
    R5 agrapa_wide P(reject by n=2000) = 0.0000 (alpha 0.05, 3 SE 0.0327)
    R5 plugin      P(reject by n=2000) = 0.0025 (alpha 0.05, 3 SE 0.0327)
    R5 mixture     P(reject by n=2000) = 0.0000 (alpha 0.05, 3 SE 0.0327)
    R3 agrapa      P(reject by n=2000) = 0.0000 (alpha 0.05, 3 SE 0.0327)
    R3 agrapa_wide P(reject by n=2000) = 0.0000 (alpha 0.05, 3 SE 0.0327)
    R3 plugin      P(reject by n=2000) = 0.0000 (alpha 0.05, 3 SE 0.0327)
    R3 mixture     P(reject by n=2000) = 0.0000 (alpha 0.05, 3 SE 0.0327)
```

Reading:

- With the default bet the block family reproduces E1's gate verdicts exactly (R2
  pooled; R3 buyers_remorse, service_failure, pooled; the rest hold).
- **The default bet's cap is the bottleneck.** In almost every regressed family the
  best constant bet on the observed costs is the largest valid one (`lambda* = 1`,
  case (ii) of 7.2): the differences are zero on most cases and large and positive on
  a few. The default stops at half of it. The wide bet multiplies the final log-wealth
  by 1.66 to 1.82 and lifts R2's resampled power at 5,000 cases from 0.37 to 0.91.
- **R5, the regression E1 missed, is a stratification problem, not only a power
  problem.** Its pooled mean is 1.34 USD minor per case (about one cent; E1's write-up
  printed it as "$1.30", a units slip) with `t = 0.27`, and the pooled floor is 509,153
  cases for any valid test. In buyers_remorse disputes the clipped mean is 64.35 minor
  per case and the floor is 689 family cases, about 3,486 benchmark cases (6,316 at the
  gate's per-hypothesis level 0.05/7). On the benchmark itself the wide bet blocks R5 on
  buyers_remorse (log E 5.11 >= 4.94); on resampled benchmarks of 10,000 cases the wide
  bet blocks it 43% of the time, the plug-in bet 39%, the default never.
- The mixture pays `log 14 = 2.64` nats for its grid and loses to the wide bet when the
  best bet is the largest one, as here.
- No strategy blocks any 500-case fold of any policy.
- Validity: on i.i.d. resamples of the centred R5 and R3 costs every bet rejected at most
  0.0025 of the time by 2,000 cases (alpha 0.05).

Caveats: resampling reuses the benchmark's own costs, so the power numbers describe
this benchmark, not agent changes in general. Raising the cap trades a little
worst-case wealth (each factor is at least 0.05 instead of 0.5) for growth; validity is
unchanged. A recommendation for the shipped default (cap 0.95 or the plug-in bet) is
reasonable on this evidence but is not made in this change.

## References

- J. Honda, A. Takemura (2010). An asymptotically optimal bandit algorithm for bounded
  support models. COLT.
- E. Kaufmann, O. Cappé, A. Garivier (2016). On the complexity of best-arm
  identification in multi-armed bandit models. JMLR 17.
- J. L. Kelly (1956). A new interpretation of information rate. Bell System Technical Journal.
- I. Waudby-Smith, A. Ramdas (2023). Estimating means of bounded random variables by
  betting. JRSS-B.
