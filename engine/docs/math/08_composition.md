# 8. what the whole promote decision guarantees (composition)

Label: **standard, applied** (intersection-union tests, Berger 1982; union bounds;
alpha spending) with one **adaptation** (per-type certificates on an interleaved
stream, 8.2). The point of this file is precision about what is and is not covered,
not new mathematics.

Code: `scruple/stats/composition.py`. Tests: `tests/test_stats_composition.py`.
Evidence: `scripts/math_composition.py`.

## Setting

A candidate is evaluated on a batch of `n` held-out cases. The gate promotes it only
if every conjunct passes:

| conjunct | what it is | its own error statement |
|---|---|---|
| `H` hard checks | deterministic functions of each episode record (ledger invariants, trace integrity, process, deadlines, evidence provenance, scope, self-report), on every case | none needed for what they read: a checked violation that is present is flagged with probability 1, if the check is correct (H3 in 05) |
| `S` soft scores | weighted quality (calibration, counterfactual cost, evidence coverage) against a threshold | none: thresholds on soft scores are heuristics |
| `B` block family | per-type plus pooled regression e-processes, weighted e-Bonferroni on running maxima (02, 06) | `P(B fires | no type regressed) <= alpha_block` |
| `T` certificate | pooled e-process for `H0: mean diff >= tolerance` reaching `1/alpha_certify` (01) | `P(T passes | mean diff >= tolerance) <= alpha_certify` |
| `D` Bayes rule (optional) | cost-weighted posterior rule (04) | none on its own; 04 shows `T and D` keeps `T`'s guarantee |

Promote `= H and S and (not B) and T (and D)`. Block `= (not H) or B`. Otherwise hold.

A *harm* is a property of the candidate we want never to ship. Each conjunct `c`
covers some harms `j` with a level `alpha_{c,j}`:
`sup over candidates with harm j of P(c passes) <= alpha_{c,j}`. A deterministic
check that reads harm `j` on every case has `alpha_{c,j} = 0`; a sampled audit has the
Theorem 5.5 bound.

## Theorem 8.1 (composition)

(a) **Promote side, intersection-union.** For a candidate with harms `J`,

    P(promote) <= min_{j in J} min_c alpha_{c,j},

and the bound is 1 when no conjunct covers any harm in `J`.

(b) **Block side, union.** For a candidate with no harm, whose hard checks raise a
false alarm on case `i` with probability `phi_i` (independently across cases),

    P(block) <= alpha_block + 1 - prod_i (1 - phi_i)  <=  alpha_block + sum_i phi_i.

Without independence, the second form still holds.

(c) **Resubmission.** If submission `k` of a candidate line uses certificate level
`alpha_k` and `sum_k alpha_k <= alpha`, then
`P(some harmful submission is ever promoted) <= alpha`. With a fixed level `alpha` per
submission the bound is `k alpha` after `k` submissions.

**Proof.** (a) `{promote}` is contained in `{c passes}` for every conjunct `c`, so
`P(promote) <= P(c passes) <= alpha_{c,j}` for every `c` and every harm `j` the
candidate has; take the minimum. This is the intersection-union principle (Berger
1982): conjunctions need no multiplicity correction. (b) `{block}` is contained in
`{B fires} or {some hard check fires}`; the first has probability at most
`alpha_block` for a harmless candidate; the second is `1 - prod (1 - phi_i)` under
independence and at most `sum phi_i` by the union bound in general. (c) The event is
the union over `k` of "submission `k` is harmful and promoted", each of probability
at most `alpha_k` by (a). QED.

## Corollary 8.2 (per-type certificates cover rare-type harm at no multiplicity cost)

Run one certificate e-process per case type `k` for `H0_k: mean_k >= t_k` on the
interleaved stream (each moves only on its own type, Lemma 2.1) and promote only if
**every** type is certified at level `alpha`. If any one type is harmful
(`mean_k >= t_k`), `P(promote) <= alpha`: by (a) with the conjunct "type `k`
certified". No `alpha/K`. The price is power: every type, including a 1% type, must
gather enough evidence on its own (03).

## What the composed decision does not guarantee

1. **Harms no conjunct covers.** E2's proxy optimum is cheaper on the simulated
   ledger (-$33.87 per case): the statistical null "the candidate costs more" is
   false, so `T` and `B` say nothing about it. Only hard checks (EVD-01, SR-01,
   PROC-01) caught it. A harm that is neither a cost regression nor read by a hard
   check (E4's unfair wins, OUT-01 is soft) is promoted with probability up to 1.
2. **Rare-type regressions under the pooled certificate.** The shipped `T` is pooled.
   A candidate that regresses a 1% type and improves the rest has pooled mean below
   tolerance, so (a) gives no bound, and the simulation below promotes it in 64% of
   runs at `n = 20,000`. `B` blocks it only if it has power. Corollary 8.2 closes this
   at a large power cost.
3. **Soft scores and the Bayes rule.** They can only remove promotions, never add
   error, but they carry no frequentist guarantee of their own.
4. **Verifier correctness.** `alpha_{H,j} = 0` assumes the check is right (gap G-4: the
   verifier and simulator share a world model). E2 is the counterexample for cost.
5. **Distribution shift** between held-out and live traffic (G-2) and **delayed
   outcomes** in canary.
6. **The block side grows with `n`.** With the honest reference's measured INV-01 rate
   `phi = 0.0019` per case, a gate that blocks on any hard failure blocks an honest
   candidate with probability about 0.32 at `n = 200` and essentially 1 at
   `n = 10,000`. Paired detection (only failures the baseline does not have on the same
   case, as in E1) reduces the effective `phi`; it does not change the arithmetic.
7. **e-BH and closed-testing flags** reported by the adapter are informative only.

## Simulation evidence (synthetic, seeded)

`uv run python scripts/math_composition.py` (a few minutes on a loaded machine). Data:
eight types with the frequencies of 02, per-case `x = mean_k + U(-0.5, 0.5)`.

```
## promote-side guarantee, 8 types (0.3, 0.2, 0.15, 0.12, 0.1, 0.08, 0.04, 0.01), alpha 0.05, reps 100
pooled: certificate on the pooled mean only; per-type: promote iff every type certified
rare type harmful (+0.35), others -0.01 (pooled mean -0.0064), n=  2000: P(promote) pooled=0.100, per-type=0.000
rare type harmful (+0.35), others -0.01 (pooled mean -0.0064), n=  8000: P(promote) pooled=0.350, per-type=0.000
rare type harmful (+0.35), others -0.01 (pooled mean -0.0064), n= 20000: P(promote) pooled=0.640, per-type=0.000
one common type at the boundary (0), others -0.05 (pooled mean -0.0350), n=  2000: P(promote) pooled=1.000, per-type=0.000
one common type at the boundary (0), others -0.05 (pooled mean -0.0350), n=  8000: P(promote) pooled=1.000, per-type=0.000
one common type at the boundary (0), others -0.05 (pooled mean -0.0350), n= 20000: P(promote) pooled=1.000, per-type=0.020
every type improves by 0.05 (pooled mean -0.0500), n=  2000: P(promote) pooled=1.000, per-type=0.000
every type improves by 0.05 (pooled mean -0.0500), n=  8000: P(promote) pooled=1.000, per-type=0.060
every type improves by 0.05 (pooled mean -0.0500), n= 20000: P(promote) pooled=1.000, per-type=0.430
every type improves by 0.15 (pooled mean -0.1500), n=  2000: P(promote) pooled=1.000, per-type=0.010
every type improves by 0.15 (pooled mean -0.1500), n=  8000: P(promote) pooled=1.000, per-type=0.960
every type improves by 0.15 (pooled mean -0.1500), n= 20000: P(promote) pooled=1.000, per-type=1.000

## block-side: honest candidate, hard-check false-alarm phi=0.0019 per case, reps 1000
statistical block family at the null boundary (every type mean 0), alpha_block 0.05
n=   200: P(block) stat=0.000 hard=0.319 either=0.319; bound 0.366
n=   500: P(block) stat=0.001 hard=0.596 either=0.597; bound 0.664
n=  2000: P(block) stat=0.002 hard=0.979 either=0.979; bound 1.000
n= 10000: P(block) stat=0.016 hard=1.000 either=1.000; bound 1.000

## resubmission: 20 harmful versions at the boundary (mean = tolerance = 0)
each on 400 fresh cases, mean + U(-0.9, 0.9); reps 600; spending sum 0.0485
after  1 submissions: P(some harmful version promoted) fixed alpha=0.027 (bound 0.05), spending=0.013 (bound 0.05)
after  5 submissions: P(some harmful version promoted) fixed alpha=0.087 (bound 0.25), spending=0.017 (bound 0.05)
after 10 submissions: P(some harmful version promoted) fixed alpha=0.205 (bound 0.50), spending=0.017 (bound 0.05)
after 20 submissions: P(some harmful version promoted) fixed alpha=0.402 (bound 1.00), spending=0.017 (bound 0.05)
```

Reading:

- **Rare-type harm is not covered by the pooled certificate.** With the 1% type
  regressing by 0.35 and the rest improving by 0.01, the pooled certificate promotes
  in 10%, 35% and 64% of runs as `n` grows from 2,000 to 20,000: more data makes the
  wrong promotion *more* likely, because the pooled mean really is below tolerance.
  Per-type certificates never promote it (0 of 100 at every `n`).
- **Per-type certificates are valid without dividing alpha.** With one common type
  exactly at its tolerance, they promote at most 2% of the time (alpha 0.05).
- **They cost power.** When every type improves by 0.05, per-type certificates
  promote 0%, 6% and 43% of the time; at 0.15, 1%, 96% and 100%. The 1% type is the
  bottleneck (80 to 200 of its cases).
- **False blocks add up.** The statistical block family stays near 0 at the null
  boundary, but a hard check with false-alarm rate 0.0019 per case blocks an honest
  candidate 32% of the time at `n = 200` and 98% at `n = 2,000`, under the 8.1(b) bound.
- **Resubmission.** Twenty harmful versions at a fixed alpha 0.05: 40% chance that one
  is promoted (bound 1.0). With Basel spending (`alpha 6/(pi^2 k^2)`): 1.7%, under 0.05.

## References

- R. L. Berger (1982). Multiparameter hypothesis testing and acceptance sampling.
  Technometrics 24(4).
- D. P. Foster, R. A. Stine (2008). Alpha-investing: a procedure for sequential
  control of expected false discoveries. JRSS-B 70(2).
- V. Vovk, R. Wang (2021). E-values: calibration, combination and applications.
  Annals of Statistics 49(3).
