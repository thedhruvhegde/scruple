# 7. variance reduction that keeps anytime validity

Labels, per result: **standard** (common random numbers, control variates with a known
mean, augmented inverse-propensity terms), **adaptation** (stratified betting on one
wealth process, predictable control-variate coefficients inside a betting e-process,
the plans), **candidate novel (minor)** (the drift-penalty correction for an estimated
covariate mean, the "learning the centre cannot pay" heuristic, and the cap-tax
budget for conditional nulls stated for the gate). "Candidate novel" means we did not
find it stated this way in a short search; it is not a claim of novelty.

Code: `scruple/stats/variance.py`; gate option `GateConfig.stats = "evalue_vr"`
(`scruple/gate/evalue.py`, `scruple/gate/sequential.py`). Tests:
`tests/test_stats_variance.py` (properties, exact one-step checks),
`tests/test_stats_variance_mc.py` (seeded Monte Carlo, fast and `slow`),
`tests/test_stats_variance_gate.py` (the gate option). Evidence: `scripts/exp_e3_mc.py`
(synthetic) and experiment E3 (`docs/experiments/E3_variance_reduction.md`, synthetic
benchmark episodes).

Notation as in 01: `X_i = (C_i - B_i)/c in [-1, 1]`, positive = candidate worse;
`F_{i-1}` everything observed before case `i`. A **pre-treatment covariate** `Z_i` is a
property of case `i` that neither policy can change (case family, amount band,
difficulty tier, network), revealed before `X_i`. The baseline's own loss `B_i` is
**not** one: conditioning on it makes two identical noisy policies look different
(regression to the mean), so it is never used as a covariate here.

## Two nulls, two tools

| decision | null | what reduces variance validly | where |
|---|---|---|---|
| block (a regression exists) | conditional: `E[X_i | F_{i-1}, Z_i] <= m(Z_i)` for every case ("no subgroup regressed") | bets that depend on `Z_i` (stratified betting, 7.1) | `StratifiedBettingEProcess` |
| promote (certificate) | marginal: `E[X_i | F_{i-1}] >= m` | control variates with a known (or bounded) covariate mean (7.3, 7.4) | `ControlVariateEProcess`, `PostStratifiedEProcess` |

A bet that depends on `Z_i` is predictable, but under the *marginal* null it is not
valid: `E[lambda(Z) (X - m)]` can be positive when `E[X] <= m` (bet big where `X` is
high). Under the *conditional* null it is valid for every bet function. For blocking,
the conditional null is also the right question: "is the candidate worse on some kind
of case" is a reason to block even if it is better on average. For promotion it is
the wrong one (rejecting "every type is worse" only shows one type is better), so the
certificate keeps the marginal null and needs a covariate whose mean is known.

## 7.0 Common random numbers (standard, measured)

The paired design already is the strongest control variate: the baseline loss `B_i` on
the same case is a covariate with coefficient 1 whose "mean" is matched exactly,
because both arms see the same case. Formally `Var(C - B) = Var C + Var B - 2 Cov(B, C)`,
so pairing helps exactly when the two losses are positively correlated (Glasserman and
Yao 1992; Asmussen and Glynn 2007, ch. V). In scruple the coupling is explicit:

- shadow replay: both policies run on the same generated cases (`generate_cases(seed)`);
- dispute episodes: the issuer's uniform draw is a function of (seed, case id, stage)
  only (`scruple.sim.issuer`), so the same case gets the same draw whatever evidence is
  submitted, and submitting more genuine evidence can never turn a win into a loss
  (a monotone coupling);
- a case whose trajectory did not change has a difference of exactly 0. The gate checks
  this invariant and fails closed when it breaks (`evalue.py`;
  `variance.crn_violations`).

`variance.crn_report` measures it: paired variance against `Var B + Var C` (two
independent samples), the correlation, and the share of exactly-zero differences.
E3 also replays the baseline with independent issuer draws to separate "same case"
from "same draws".

## 7.1 and 7.2 Stratified betting on one wealth process (adaptation)

Types `k_i` (any pre-treatment covariate with finitely many values), null means `m_k`,
and per-type predictable bets `lambda_{k,i}` in `[0, b/(1 + s m_k)]`, `b < 1`. Let

    K_t = prod_{i<=t} (1 + lambda_{k_i, i} s (X_i - m_{k_i})).

**Proposition 7.1.** Under `H0^cap: E[X_i | F_{i-1}, k_i] <= m_{k_i}` for every `i` (for
`s = +1`), `K` is a non-negative supermartingale with respect to the filtration that
reveals `k_i` before `X_i`. So `P(sup_t K_t >= 1/alpha) <= alpha` and `K_tau` is an
e-value at every stopping time.

**Proof.** Every factor is at least `1 - b > 0`. Conditionally on `F_{i-1}` and `k_i`,
`lambda_{k_i, i}` is fixed and non-negative, so the factor has mean
`1 + lambda s (E[X_i | F_{i-1}, k_i] - m_{k_i}) <= 1`. Average over `k_i` (tower
property), then Ville and optional stopping as in 01, Proposition 1.1. QED.

Nothing is assumed about how types arrive, their shares, or the dependence between
types. Per-type aGRAPA bets (01) are used, with one change: a type is bet on only once
its running mean of `Y` is at least `activation` standard errors above zero (default
1). A plug-in bet on a type that sits at or below its null loses about
`log(n_k)/2` over `n_k` cases; over many such types this outweighs the `log K` the
stratified test saves, and in synthetic runs plain per-type aGRAPA lost to
e-Bonferroni when five of six types improved slightly. The activation rule is
predictable, so validity is unchanged; it costs power when a regression is spread
thinly over every type, which the pooled member of the base family covers. The
default (1 standard error) and the share `w = 0.8` below were fixed from synthetic
Monte Carlo before experiment E3 ran. `contributions` splits `log K_t` by type.

**Proposition 7.2 (why it is more powerful).** (a) With constant bets, the best growth
rate of the stratified process, `sup_{lambda_1..lambda_K} sum_k pi_k E_k log(1 + lambda_k Y)`,
is at least the pooled one, `sup_lambda E log(1 + lambda Y)` (take all `lambda_k`
equal). (b) To second order (01, Lemma 3.1 of 03) the two are
`sum_k pi_k (mu_k^+)^2 / (2 M2_k)` and `(mu^+)^2 / (2 M2)` with `mu = sum pi_k mu_k`,
`M2 = sum pi_k M2_k`; by Cauchy-Schwarz the first is at least the second, with
equality only if `mu_k / M2_k` is the same for every type. (c) Against e-Bonferroni of
`K` per-type processes, it needs `log(1/alpha)` instead of `log(K/alpha)`, and adds up
evidence that is spread over several types.

**Proof.** (a) is a restriction of the maximisation. (b): Cauchy-Schwarz,
`(sum pi_k mu_k^+)^2 <= (sum pi_k (mu_k^+)^2 / M2_k)(sum pi_k M2_k)`, and
`mu^+ <= sum pi_k mu_k^+`. (c) is the threshold. QED. The between-type variance and
the cancellation of an improving type against a regressing one both drop out.

What it gives up: it rejects "no type regressed" without naming the type. The gate
therefore spends `w alpha` on it (default `w = 0.8`) and `(1 - w) alpha` on the existing
family, which names types (e-BH in the shadow gate, e-Bonferroni of per-type and pooled
processes in `evaluate_pairs`): `P(false block) <= w alpha + (1 - w) alpha = alpha` when
no type regressed.
`contributions` shows which type carried the wealth (reported, not a guarantee).

Closest prior work we found: products of per-stratum test supermartingales for
intersection hypotheses in stratified election audits (Spertus and Stark 2022,
"Sweeter than SUITE", arXiv 2207.03379); there the strata are sampled separately,
here they arrive interleaved on one stream. A literature check is required.

## 7.3 Control variates with a predictable coefficient (standard idea, adaptation)

Covariates `Z_i in [-1, 1]^p` with a **known** conditional mean `nu = E[Z_i | F_{i-1}]`
(i.i.d. cases from a population whose covariate mean is known: stratum shares fixed by
design, or a generator with a published mix). Coefficient `theta_i` predictable (ridge
least squares of `X` on `Z` from cases `1..i-1`), scaled so `||theta_i||_1 <= theta_max`.

    R_i = X_i - theta_i . (Z_i - nu),   K_t = prod (1 + lambda_i s (R_i - m)),
    0 <= lambda_i <= b / (1 + s m + 2 theta_max).

**Proposition 7.3.** Under the marginal null `E[X_i | F_{i-1}] <= m` (for `s = +1`), `K`
is a non-negative supermartingale. The bet sees a second moment of about
`Var(X)(1 - rho^2)` instead of `Var(X)` for the oracle linear coefficient.

**Proof.** `|theta . (Z - nu)| <= ||theta||_1 max_j |Z_j - nu_j| <= 2 theta_max`, so every
factor is at least `1 - b`. `E[R_i | F_{i-1}] = E[X_i | F_{i-1}] - theta_i . (E[Z_i | F_{i-1}] - nu) = E[X_i | F_{i-1}]`
because `theta_i` is `F_{i-1}`-measurable. Then 01, Proposition 1.1. The variance is the
usual control-variate identity. QED.

This is the CUPED adjustment (Deng, Xu, Kohavi and Walker 2013) with two changes that
keep anytime validity: the coefficient is estimated from the past only (so it is
predictable, as the plug-in bets of Waudby-Smith and Ramdas 2023 are), and the
covariate mean must be known rather than estimated from the same sample. Lindon, Ham,
Tingley and Bojinov (2022, arXiv 2210.08589) give regression-adjusted anytime-valid
tests for randomised experiments under Gaussian linear models; ours is
nonparametric for bounded data and for a paired design. **Post-stratification**
(`PostStratifiedEProcess`) is the special case `Z = one-hot(type) - shares` with
`theta_k` the running mean of type `k`: `R_i = X_i - theta_{k_i} + sum_j pi_j theta_j`.

## 7.4 Estimated covariate mean: the drift penalty (candidate novel, minor)

If `nu` is only known to lie within `r_j` of a centre `c_{i,j}` (an interval at level
`a` from an independent pre-period, or a time-uniform interval from the stream itself,
`variance.mixture_radius`), use

    Y_i = s (X_i - theta_i . (Z_i - c_i) - m) - sum_j |theta_{ij}| r_{ij}.

**Proposition 7.4.** Let `A` be the event that `|nu_j - c_{i,j}| <= r_{i,j}` for all `i, j`,
with `P(A^c) <= a`. Then `P(sup_t K_t >= 1/alpha) <= alpha + a`.

**Proof.** On `A_{i-1}` (the event up to case `i`), `E[Y_i | F_{i-1}] <= s(mu_i - m) + sum_j |theta_ij| |nu_j - c_ij| - sum_j |theta_ij| r_ij <= 0`.
Let `tau` be the first `i` at which `A_i` fails (a stopping time). `K_{t ^ tau}` is a
non-negative supermartingale, so `P(sup_t K_t >= 1/alpha) <= P(sup_t K_{t ^ tau} >= 1/alpha) + P(tau < inf) <= alpha + a`. For the running centre, the interval is the normal-mixture boundary for
1-sub-Gaussian increments (Robbins 1970; Howard, Ramdas, McAuliffe and Sekhon 2021),
union-bounded over coordinates. QED. Here `K` is not an e-process; it is a level-`(alpha + a)` test.

**Heuristic (why learning the centre from the same stream does not pay).** With one
covariate, correlation `rho`, the oracle `theta = rho sigma_X / sigma_Z` and a radius of
about `sigma_Z sqrt(2 log(1/a) / t)`, the penalty is about
`rho sigma_X sqrt(2 log(1/a) / t)`. A regression that is just detectable at `t` has a
mean of the same order, `sigma_X sqrt(2 log(1/alpha) / t)`. So near the detection
boundary the penalty eats about a share `rho` of the drift while the variance falls by
`1 - rho^2`: the growth rate changes by about `(1 - rho)^2 / (1 - rho^2) = (1 - rho)/(1 + rho) < 1`.
A control variate helps only when its mean comes from elsewhere (a design, or a
pre-period much longer than the test). This matches the Monte Carlo below.

## 7.5 Centred inverse-propensity canary terms (standard, applied)

In the canary (06, `scruple/gate/sequential.py`) each matched case is treated with a
known probability `p`. With a prediction `f_i in [0, c]` fixed before the assignment,

    Z_i = (Y_i - f_i) (T_i / p - (1 - T_i) / (1 - p))

has `E[Z_i | F_{i-1}, Y_i(0), Y_i(1)] = Y_i(1) - Y_i(0)` because `T_i` is independent of
`f_i` and of the potential costs; `|Y_i - f_i| <= c` keeps the scale `M`. This is the
augmented inverse-propensity form (Robins, Rotnitzky and Zhao 1994) with a predictable
outcome model, as used for anytime-valid off-policy inference by Waudby-Smith, Wu,
Ramdas, Karampatziakis and Mineiro (2022, arXiv 2210.10768). The assignment
indicator is a control variate whose mean `p` is known exactly by design, which is the
setting where 7.3 needs no extra assumption. `evalue_vr` uses `f_i` = the running mean
cost of earlier matched cases of the type (both arms). The second moment falls from
`E[Y^2]` to `E[(Y - f)^2]`, a large gain when costs sit far from zero.

## 7.6 Planning with the variance reduction (adaptation)

- Stratified (block): a regression of `Delta_k` in type `k` alone is flagged with power
  `1 - beta` by the fixed-bet version that bets only on type `k` once `n_k` reaches
  the 03 plan at level `w alpha` (not `alpha / K`): `variance.stratified_cases_needed`.
  The adaptive version also bets on the other types; at the null boundary their
  aGRAPA bets cost a lower-order amount, measured by the Monte Carlo.
- The gate plan (`evalue_vr`) takes per type the smaller of the stratified plan at
  `w alpha` and the e-BH plan at `(1 - w) alpha / K`.
- Control variates (certificate): the 03 plan with the residual sd `sd sqrt(1 - R^2)`
  and the cap widened to `c (1 + 2 theta_max)` for the larger bet range
  (`variance.cv_cases_needed`). `R^2` is an ASSUMPTION fixed from history before
  outcomes; the plan uses the oracle coefficient, which the predictable estimate
  approaches.

## 7.7 The cap tax survives variance reduction (candidate novel framing; standard pieces)

**Proposition 7.7.** Let case `i` have a declared cap `c_i`, and let the null be the
conditional one (every law on `[-c_i, c_i]` with conditional mean `<= 0`). For any
alternative under which case `i` has conditional mean `mu_i`, every e-value `E` for that
null satisfies

    E_1[log E] <= sum_i log(1 + mu_i^+ / c_i) <= sum_i mu_i^+ / c_i,

and every level-`alpha` test with power `1 - beta` has
`kl(1 - beta || alpha) <= sum_i log(1 + mu_i^+ / c_i)`.

**Proof.** For a law `P` of case `i` with mean `mu > 0` on `[-c, c]`, the law
`P0 = (1 - p) P + p delta_{-c}` with `p = mu / (mu + c)` has mean 0, so it is in the
null, and `dP/dP0 <= 1/(1 - p)`, so `KL(P || P0) <= log(1 + mu / c)`. For `mu <= 0` take
`P0 = P`. The product of these laws (conditionally on the past) is in the null, and
`E_1[log E] <= KL(P1 || P0) + log E_0[E] <= KL(P1 || P0)` (Gibbs' inequality; this is
the growth-rate bound of Grünwald, de Heide and Koolen 2024). KL adds over cases by
the chain rule. The test bound is data processing as in 03, Proposition 3.3. QED.

Consequence: stratification can raise `sum_i mu_i^+` (an improving type no longer
cancels a regressing one), and control variates can shrink the variance, but no
valid method can push the expected log-wealth above the sum of conditional
regressions over caps. Power `1 - beta` at level `alpha` needs the budget
`B = sum_i mu_i^+ / c_i` to reach at least `kl(1 - beta || alpha)`: at `alpha = 0.05`,
0.83 for power 0.5 and 1.91 for power 0.8; at `alpha = 0.04` (the stratified share
`w = 0.8` in the gate), 0.94 and 2.08. With one cap `c` and no improving subgroup,
`B = n mu / c`. E3 applies this to E1's R5.

## Simulation evidence (synthetic, seeded)

Pasted from `uv run python scripts/exp_e3_mc.py` (added with the Monte Carlo commit).

## References

- J. Ville (1939). Étude critique de la notion de collectif. Gauthier-Villars.
- H. Robbins (1970). Statistical methods related to the law of the iterated logarithm.
  Annals of Mathematical Statistics 41(5).
- J. M. Robins, A. Rotnitzky, L. P. Zhao (1994). Estimation of regression coefficients
  when some regressors are not always observed. JASA 89(427).
- P. Glasserman, D. D. Yao (1992). Some guidelines and guarantees for common random
  numbers. Management Science 38(6).
- S. Asmussen, P. W. Glynn (2007). Stochastic Simulation: Algorithms and Analysis. Springer.
- A. Deng, Y. Xu, R. Kohavi, T. Walker (2013). Improving the sensitivity of online
  controlled experiments by utilizing pre-experiment data. WSDM.
- S. R. Howard, A. Ramdas, J. McAuliffe, J. Sekhon (2021). Time-uniform, nonparametric,
  nonasymptotic confidence sequences. Annals of Statistics 49(2).
- M. Lindon, D. W. Ham, M. Tingley, I. Bojinov (2022). Anytime-valid linear models and
  regression adjusted causal inference in randomized experiments. arXiv 2210.08589.
- J. Spertus, P. B. Stark (2022). Sweeter than SUITE: supermartingale stratified
  union-intersection tests of elections. arXiv 2207.03379.
- I. Waudby-Smith, A. Ramdas (2023). Estimating means of bounded random variables by
  betting. JRSS-B.
- I. Waudby-Smith, L. Wu, A. Ramdas, N. Karampatziakis, P. Mineiro (2022). Anytime-valid
  off-policy inference for contextual bandits. arXiv 2210.10768.
- P. Grünwald, R. de Heide, W. Koolen (2024). Safe testing. JRSS-B.
