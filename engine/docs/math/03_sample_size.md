# 3. sample-size and power planning

Label: **adaptation** (fixed-bet growth bound plus Bernstein, solved exactly; exact
binomial counts; optimised alpha allocation) and **standard, applied** (the KL lower
bound, Proposition 3.3).

Code: `scruple/stats/planning.py`. Tests: `tests/test_stats_planning.py`.
Evidence: `scripts/math_planning.py`, `scripts/math_gate_demo.py`.

## Question

Given type frequencies `pi_k`, caps `c_k`, tolerances `t_k`, a minimum detectable
regression `Delta_k` and a per-case standard deviation bound `sigma_k` (all money in USD
minor units), how many held-out cases `N` (or canary days) are needed so that a
regression of at least `Delta_k` in type `k` is flagged with probability at least
`(1 - beta)(1 - gamma)` while the FWER stays at most `alpha`?

Normalised: `m = t_k / c_k`, `d = Delta_k / c_k`, `sigma = sigma_k / c_k`
(worst case `sigma^2 = 1 - (m + d)^2` if no bound is given), `Y = X - m`,
`M2 = E Y^2 = sigma^2 + d^2`, constant bet `lambda`, `a = lambda (1 + m) < 1`.

## Lemma 3.1 (growth bound)

For `a in [0, 1)` and `x >= -a`: `log(1 + x) >= x - x^2 / (2 (1 - a))`. Hence

    g(lambda) := E log(1 + lambda Y) >= lambda d - lambda^2 M2 / (2 (1 - a)).

**Proof.** `f(x) = log(1 + x) - x + x^2/(2(1 - a))` has `f(0) = 0` and
`f'(x) = x (1/(1 - a) - 1/(1 + x))`, which is `<= 0` on `[-a, 0]` and `>= 0` on
`[0, inf)`. Apply with `x = lambda Y >= -a`. QED. (Checked against exact growth for
random three-point laws, hypothesis.)

## Proposition 3.2 (sufficient stratum sample size)

Let `Z = log(1 + lambda Y)`. Then `Var Z <= v := lambda^2 M2 / (1 - a)^2` (from
`|log(1 + x)| <= |x|/(1 - a)` on `x >= -a`) and the lower deviation `E Z - Z` is at most
`b := lambda d - log(1 - a)` (since `E Z <= lambda d`). By Bernstein's inequality
(e.g. Boucheron, Lugosi and Massart 2013, "Concentration Inequalities"),
`P(S_n <= n E Z - eps) <= beta` for `eps = l b/3 + sqrt((l b/3)^2 + 2 l n v)`,
`l = log(1/beta)`. Since `sup_{t<=n} S_t >= S_n` and `E Z >= g(lambda)`, the fixed-bet
e-test at level `alpha_k` has power at least `1 - beta` by `n` stratum cases whenever

    n g(lambda) - eps(n, lambda) >= log(1/alpha_k).

The planner returns the smallest such `n` (bisection on `n`) for the best `lambda` (grid
plus golden section). `method="hoeffding"` uses the range instead of the variance and
is much looser; `method="clt"` (`eps = z_{1-beta} lambda sigma sqrt(n)`) is an
approximation, not a guarantee. The bound holds for every law with mean `m + d` and
variance at most `sigma^2` (and for larger regressions while `d' <= (1 - a)/lambda`).

**Counts.** With `N` total cases, `n_k ~ Binomial(N, pi_k)`. We compute exactly the
smallest `N` with `P(Binomial(N, pi_k) >= n_k*) >= 1 - gamma` (tail sums with the pmf
ratio, checked against brute force). For i.i.d. traffic the count is independent of the
type-`k` outcomes and power is non-decreasing in `n`, so power is at least
`(1 - beta)(1 - gamma)`. The total is `max_k N_k`; canary days are
`N / (daily cases x canary fraction)`.

**Allocation.** e-Bonferroni levels `alpha_k = alpha w_k / K`, `sum alpha_k = alpha`.
Uniform: `alpha/K` each. Optimised: the smallest `N` with
`sum_k exp(-L_k(N)) <= alpha`, where `L_k(N)` is the level stratum `k` reaches with power
`1 - beta` from its guaranteed count; then `alpha_k` proportional to `exp(-L_k(N))`.
Rare strata receive most of the budget.

## Proposition 3.3 (the cap tax: a lower bound for any valid test)

Let `P1` be the point mass at `m + d` and `P0` put `1 - p` on `m + d` and
`p = d/(1 + m + d)` on `-1`. `P0` has mean `m`, so it is in the null of every test that is
valid for all laws on `[-1, 1]` with mean `<= m`. `KL(P1 || P0) = log(1 + d/(1 + m))`.
Any such test with level `alpha` and power `1 - beta` after `n` observations satisfies

    n >= kl(1 - beta || alpha) / log(1 + d/(1 + m))  ~  log(1/alpha) / d,

and for sequential tests the same holds for the expected stopping time under `P1`.

**Proof.** Data processing for KL applied to the rejection event gives
`n KL(P1 || P0) >= kl(P1(rej) || P0(rej)) >= kl(1 - beta || alpha)` (binary `kl` is
monotone in each argument away from the diagonal). The sequential version is the
change-of-measure lemma of Kaufmann, Cappé and Garivier (2016, "On the complexity of
best-arm identification in multi-armed bandit models", JMLR, Lemma 1). QED.

Consequence: because `d = Delta/c`, the required sample size grows linearly with the
declared cap. A loose cap is paid for in cases, by every valid nonparametric test, not
only by ours. The rigorous plans are checked to sit above this bound (hypothesis).

## Simulation evidence (synthetic, seeded)

Illustrative inputs (ASSUMPTION, synthetic): cap $100, MDE $5 per case, per-case sd
$20, tolerance $0, eight strata as in 02, `alpha = 0.05` FWER, `beta = 0.2`,
`gamma = 0.05`. Validation uses moment-matched three-point laws.

```
## one stratum: cap $100, MDE $5, sd $20, alpha_k = 0.05/8, beta = 0.2
hoeffding: n_k*= 2664, bet=0.218, simulated power fixed-bet=1.000, aGRAPA=1.000
bernstein: n_k*=  795, bet=0.300, simulated power fixed-bet=1.000, aGRAPA=1.000
clt      : n_k*=  453, bet=0.421, simulated power fixed-bet=0.984, aGRAPA=0.991
simulated minimal n (fixed CLT bet, 1000 reps, +-5): about 341

## eight strata (0.3, 0.2, 0.15, 0.12, 0.1, 0.08, 0.04, 0.01) alpha=0.05 FWER, beta=0.2, gamma=0.05
bernstein uniform  : total cases N= 84170 (binding s7), canary days at 2000/day x 10% = 420.9; rare stratum alpha_k=0.0063, n_k*=795
bernstein optimised: total cases N= 60446 (binding s0), canary days at 2000/day x 10% = 302.2; rare stratum alpha_k=0.0500, n_k*=565
clt       uniform  : total cases N= 48839 (binding s7), canary days at 2000/day x 10% = 244.2; rare stratum alpha_k=0.0063, n_k*=453
clt       optimised: total cases N= 31738 (binding s1), canary days at 2000/day x 10% = 158.7; rare stratum alpha_k=0.0500, n_k*=289

## validate optimised bernstein plan by simulation: N=60446, reps=200
per-stratum power at the MDE: s0=1.000, s1=1.000, s2=1.000, s3=1.000, s4=1.000, s5=1.000, s6=1.000, s7=1.000  (guarantee >= 0.76)
FWER with every stratum at the null boundary: 0.0000 (alpha 0.05)
```

Reading: the Bernstein plan (795 rare-type cases) is valid but about 2.3 times the
simulated minimum (about 341 at the CLT bet); the CLT plan is close to the simulated
minimum here but carries no guarantee. Optimised allocation cuts total traffic by
about 28% (Bernstein) to 35% (CLT) relative to uniform `alpha/K`. The rare 1% type
binds: at 2000 cases per day with a 10% canary, detecting a $5 regression in it takes
hundreds of days, which is the real cost of guaranteeing rare-type safety.

On the demo's own held-out data (06), detecting the A-1 regression in the
late-evidence slice ($10.79 per case, sd $6.76, 5.9% of traffic, declared cap $10,300)
needs about 5,000 to 8,700 late-evidence cases; Proposition 3.3 says no valid test
could do it with fewer than about 3,500. The demo has 178.

## What is known vs new

Lemma 3.1 is elementary; Bernstein and the KL lower bound are standard. Solving the
fixed-bet power condition exactly, combining it with exact binomial counts and the
optimised e-Bonferroni allocation is, as far as we know, an adaptation rather than a
new result. A literature check is required (sample-size planning for betting tests
is discussed in the safe-testing literature, e.g. Grünwald, de Heide and Koolen,
"Safe testing", JRSS-B 2024).

## References

- W. Hoeffding (1963). Probability inequalities for sums of bounded random variables. JASA.
- S. Boucheron, G. Lugosi, P. Massart (2013). Concentration Inequalities: A Nonasymptotic
  Theory of Independence. Oxford University Press.
- E. Kaufmann, O. Cappé, A. Garivier (2016). On the complexity of best-arm identification
  in multi-armed bandit models. JMLR 17.
- P. Grünwald, R. de Heide, W. Koolen (2024). Safe testing. JRSS-B.
