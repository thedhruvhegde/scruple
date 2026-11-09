# 2. stratified guarantees for rare, expensive case types

Label: **standard, applied** (e-Bonferroni, e-BH, closed testing with e-values), plus an
**adaptation**: per-stratum e-processes on one interleaved stream, with time-uniform
weighted e-Bonferroni for continuous monitoring.

Code: `scruple/stats/stratified.py`. Tests: `tests/test_stats_stratified.py`.
Evidence: `scripts/math_stratified.py`.

## Setting

Cases arrive in one stream; each has a type `k in {1..K}` (dispute scenario, or
scenario by amount band) with frequency `pi_k`, possibly 1% or less. For each type the
null is

    H0_k : E[X | past, type = k] <= m_k     ("type k did not regress beyond m_k")

tested by a betting e-process `E_k` (01) that moves only when a type-`k` case arrives.
The family question is "which case types regressed?", with control of the family-wise
error (FWER: any false "regressed") or the false discovery rate (FDR).

## Lemma 2.1 (stratum e-processes on a shared stream)

Under `H0_k`, `E_k` is a non-negative supermartingale with respect to the global
filtration of the interleaved stream, so `E_{k,tau}` is an e-value for every global
stopping time `tau`, and `P(sup_t E_{k,t} >= c) <= 1/c`.

**Proof.** At a type-`k` arrival the factor is `1 + lambda Y` with
`E[1 + lambda Y | past, type = k] <= 1` (01, Proposition 1.1, conditioning on the type,
which is known before the outcome). At any other arrival the factor is exactly 1.
Ville and optional stopping finish it. QED.

## Proposition 2.2 (time-uniform weighted e-Bonferroni, FWER)

Weights `w_k >= 0`, `sum w_k = K`. Flag type `k` the first time
`E_{k,t} >= K / (alpha w_k)`. Then `P(any true null is ever flagged) <= alpha` under
arbitrary dependence between types.

**Proof.** Union bound over true nulls of Lemma 2.1 with `c = K/(alpha w_k)`:
`sum_{k null} alpha w_k / K <= alpha`. QED. Weights let rare, expensive types get a
larger share of `alpha` (03 shows how to choose them).

## Proposition 2.3 (closed testing with mean e-values, FWER)

At a stopping time, reject `H0_k` iff `mean_{j in S} E_j >= 1/alpha` for every `S`
containing `k`. FWER `<= alpha` under arbitrary dependence; it rejects everything
unweighted e-Bonferroni rejects; and the minimum over `S` is attained by `k` plus the
`s - 1` smallest other e-values, so it costs `O(K log K)` per type.

**Proof.** For the set `I0` of true nulls, `mean_{I0} E_j` is an e-value (linearity),
so `P(mean_{I0} E_j >= 1/alpha) <= alpha` (Markov). Any false rejection requires this
event (closed testing, Marcus, Peritz and Gabriel 1976). Dominance: if
`E_k >= K/alpha` then every `mean_S >= E_k/|S| >= 1/alpha`. Shortcut: the mean of a
set of fixed size containing `k` is smallest when the other members are the smallest
e-values. The code is checked against brute force over all subsets (hypothesis). QED.

The arithmetic mean as an e-merging function is studied by Vovk and Wang (2021, "E-values:
calibration, combination and applications", Annals of Statistics).

## Proposition 2.4 (weighted e-BH, FDR)

At a stopping time, with `e'_k = w_k E_k`, reject the `r` largest where `r` is the
largest `j` with `e'_(j) >= K/(alpha j)`. Then `FDR <= alpha sum_{k null} w_k / K <= alpha`
under arbitrary dependence (Wang and Ramdas 2022, "False discovery rate control with
e-values", JRSS-B 84(3)).

**Proof.** With `R` rejections, each rejected `k` has `e'_k >= K/(alpha R)`, so
`1{k rejected}/max(R, 1) <= alpha e'_k / K`. Sum over true nulls and take expectations;
`E[E_k] <= 1`. QED.

**Caveat.** 2.3 and 2.4 hold at one stopping time. "Reject whenever e-BH has ever
rejected" over a monitored stream is not covered by 2.4; use 2.2 while monitoring and
run 2.3 or 2.4 at the decision time.

## Demonstration: pooled tests miss rare-type regressions

Eight types with frequencies `(0.30, 0.20, 0.15, 0.12, 0.10, 0.08, 0.04, 0.01)`. The
rare type (1% of traffic) regresses by `+0.35` cap units per case; every other type
improves by `0.01`. The pooled mean improves (`-0.0064`), so a pooled regression test,
however valid, has nothing to detect, while the stratified test flags the rare type.

The table also shows the current gate's "no slice mean may get worse" check (zero
tolerance) at the null boundary (`mu_k = 0` exactly): it has no error control and
false-blocks almost always, which quantifies verifier gap G-7.

```
## error control, K=8 strata, freqs (0.3, 0.2, 0.15, 0.12, 0.1, 0.08, 0.04, 0.01) alpha=0.1, n=4000, reps=1000
regressed=()           comonotone=False FWER e-Bonf(sup)=0.0260 FWER closed=0.0020 FDR e-BH=0.0010 (bound 0.100) naive-slice-mean>0 FWER=0.996 P(detect>=1 true)=0.000
regressed=()           comonotone=True  FWER e-Bonf(sup)=0.0080 FWER closed=0.0050 FDR e-BH=0.0040 (bound 0.100) naive-slice-mean>0 FWER=0.830 P(detect>=1 true)=0.000
regressed=(0,)         comonotone=False FWER e-Bonf(sup)=0.0210 FWER closed=0.0010 FDR e-BH=0.0015 (bound 0.088) naive-slice-mean>0 FWER=0.993 P(detect>=1 true)=1.000
regressed=(0,)         comonotone=True  FWER e-Bonf(sup)=0.0040 FWER closed=0.0030 FDR e-BH=0.0017 (bound 0.088) naive-slice-mean>0 FWER=0.839 P(detect>=1 true)=1.000
regressed=(6, 7)       comonotone=False FWER e-Bonf(sup)=0.0300 FWER closed=0.0030 FDR e-BH=0.0023 (bound 0.075) naive-slice-mean>0 FWER=0.984 P(detect>=1 true)=0.950
regressed=(6, 7)       comonotone=True  FWER e-Bonf(sup)=0.0040 FWER closed=0.0020 FDR e-BH=0.0021 (bound 0.075) naive-slice-mean>0 FWER=0.731 P(detect>=1 true)=0.933
regressed=(0, 1, 2, 3) comonotone=False FWER e-Bonf(sup)=0.0110 FWER closed=0.0030 FDR e-BH=0.0014 (bound 0.050) naive-slice-mean>0 FWER=0.936 P(detect>=1 true)=1.000
regressed=(0, 1, 2, 3) comonotone=True  FWER e-Bonf(sup)=0.0030 FWER closed=0.0050 FDR e-BH=0.0022 (bound 0.050) naive-slice-mean>0 FWER=0.729 P(detect>=1 true)=1.000

## pooled vs stratified
rare stratum s7 (1% of traffic) mean +0.35, others -0.01; pooled mean -0.0064
n=8000, reps=500: pooled anytime test flags regression 0.006; stratified e-Bonferroni flags s7 1.000
```

`comonotone=True` drives every type from the same uniform draws, a strong positive
dependence; all guarantees hold under arbitrary dependence and the numbers agree.

## What is known vs new

The procedures and proofs are standard (Vovk and Wang 2021; Wang and Ramdas 2022;
Marcus et al. 1976). The application (per-case-type agent-change regression with
interleaved, continuously monitored strata) is ours. A literature check is required,
in particular against work on sequential subgroup testing in online experiments.

## References

- R. Marcus, E. Peritz, K. R. Gabriel (1976). On closed testing procedures with special
  reference to ordered analysis of variance. Biometrika 63(3).
- V. Vovk, R. Wang (2021). E-values: calibration, combination and applications. Annals
  of Statistics 49(3).
- R. Wang, A. Ramdas (2022). False discovery rate control with e-values. JRSS-B 84(3).
