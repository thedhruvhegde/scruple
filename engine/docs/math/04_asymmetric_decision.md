# 4. cost-weighted asymmetric decision rule, composed with the certificate

Label: **standard, applied** (Bayes rule under asymmetric loss) and **adaptation**
(composition with an e-value certificate, with constrained Bayes optimality).

Code: `scruple/stats/decision.py`. Tests: `tests/test_stats_decision.py`.
Evidence: `scripts/math_decision.py`.

## Setting

`theta` is the true mean per-case loss difference (candidate minus baseline; positive
is worse), `delta` the tolerance. Actions: promote or block.

- `zero_one` loss: a wrong approval (promote while `theta > delta`) costs `c_A`; a wrong
  block (block while `theta <= delta`) costs `c_B`.
- `linear` loss: promoting costs `c_A V (theta - delta)^+` (excess loss over `V` cases
  until the next review); blocking costs `c_B V (delta - theta)^+` (forgone savings).

For money-moving agents `c_A >> c_B` is typical, but the ratio is the owner's call
(ASSUMPTION in every example here).

## Proposition 4.1 (Bayes rule)

Under a posterior `Pi`, the rule minimising posterior expected loss promotes iff
`rho_P <= rho_B`. For `zero_one` this is

    Pi(theta > delta) <= p* = c_B / (c_A + c_B);

for `linear`, `c_A E(theta - delta)^+ <= c_B E(delta - theta)^+`. With a normal posterior
`N(mu, s^2)` and `z = (mu - delta)/s`: `E(theta - delta)^+ = (mu - delta) Phi(z) + s phi(z)`
and `E(delta - theta)^+ = (delta - mu) Phi(-z) + s phi(z)`.

**Proof.** Bayes risk `= int [phi(x) rho_P(x) + (1 - phi(x)) rho_B(x)] dm(x)` by Fubini,
minimised pointwise. The normal formulas are the standard partial expectations (checked
against quadrature). QED. Standard (Berger 1985).

The posterior used is the conjugate normal approximation: prior `N(mu0, tau^2)` on
`theta`, likelihood `theta_hat ~ N(theta, se^2)` from the paired differences. That is a
CLT approximation; nothing in the frequentist guarantee below depends on it.

## Proposition 4.2 (composition)

Let `E` be an e-value for `H0: theta >= delta` (the certificate of 01, stopped or as a
running maximum via Ville) and `A = {E >= 1/alpha}`. The composed rule promotes iff
`x in A` and the Bayes rule promotes. Then:

1. for every `theta >= delta`, `P_theta(promote) <= alpha`, whatever the prior and
   whether or not the normal approximation is right;
2. among all rules `phi <= 1_A` (never promote without the certificate), the composed
   rule minimises Bayes risk under the model;
3. hence its Bayes risk is at most that of the certificate alone (`phi = 1_A`), and its
   excess over the unconstrained Bayes rule is `int_{A^c} (rho_B - rho_P)^+ dm`.

**Proof.** (1) `promote ⊆ A` and `P_theta(A) <= alpha` (Markov, or Ville for the
running maximum). (2) Restrict the pointwise minimisation of 4.1 to `x in A`; off `A`
the only allowed action is block. (3) `1_A` is in the class; the excess follows by
comparing pointwise. QED.

**Calibrating alpha from costs.** Under `zero_one`, (1) caps the expected
wrong-approval cost at `alpha c_A` for every harmful `theta`; a budget `b` gives
`alpha = b / c_A` (`alpha_for_budget`). Grünwald (2024, "Beyond Neyman-Pearson: e-values
enable hypothesis testing with a data-driven alpha", PNAS) shows that with e-values
`alpha` may even be chosen after seeing the data; we do not use that here.

## Simulation evidence (synthetic, seeded)

Prior `N(-0.01, 0.03^2)` in cap units (most changes help a little; ASSUMPTION),
tolerance 0, ternary data with 30% of cases changed, `n = 300` per decision. "freq" is
the certificate alone; "block" never promotes. The last column is the frequentist error
at the boundary `theta = delta`. "posterior-expected risk" is the model's own Bayes
risk of the chosen action (lower variance than realised loss; Proposition 4.2(2) holds
for it exactly, which the tests assert).

```
## prior N(-0.01, 0.03^2) on theta (normalised), tolerance 0, ternary data p_changed=0.3 |x|=0.8, reps=10000 per row
c_A:c_B  kind      alpha  n    p*      | Bayes risk: bayes / freq / composed / block | P(promote | theta=delta): bayes / freq / composed
  100:1   zero_one  0.1   300  0.0099 | 0.5686 / 0.5633 / 0.5724 / 0.6246 | 0.0032 / 0.0033 / 0.0026 | SE(freq - composed)=0.0009
                                posterior-expected risk: 0.5889 / 0.5918 / 0.5894 / 0.6303
  100:1   zero_one  0.05  300  0.0099 | 0.5954 / 0.6008 / 0.6012 / 0.6358 | 0.0030 / 0.0017 / 0.0017 | SE(freq - composed)=0.0002
                                posterior-expected risk: 0.5923 / 0.5960 / 0.5959 / 0.6363
   10:1   zero_one  0.05  300  0.0909 | 0.4665 / 0.5717 / 0.5717 / 0.6227 | 0.0708 / 0.0013 / 0.0013 | SE(freq - composed)=0.0000
                                posterior-expected risk: 0.4737 / 0.5828 / 0.5828 / 0.6334
    1:1   zero_one  0.05  300  0.5000 | 0.2080 / 0.5761 / 0.5761 / 0.6268 | 0.6137 / 0.0017 / 0.0017 | SE(freq - composed)=0.0000
                                posterior-expected risk: 0.2101 / 0.5789 / 0.5789 / 0.6298
    1:10  zero_one  0.05  300  0.9091 | 0.3478 / 5.7721 / 5.7721 / 6.2990 | 0.9772 / 0.0017 / 0.0017 | SE(freq - composed)=0.0000
                                posterior-expected risk: 0.3359 / 5.8105 / 5.8105 / 6.3370
   10:1   linear    0.05  300  0.0909 | 0.7485 / 1.4621 / 1.4621 / 1.7793 | 0.1881 / 0.0017 / 0.0017 | SE(freq - composed)=0.0000
                                posterior-expected risk: 0.7324 / 1.4669 / 1.4669 / 1.7824

## row 1 again with reps=50000: realised 0.5897 / 0.5942 / 0.5873; posterior-expected 0.5885 / 0.5912 / 0.5890; SE(freq - composed)=0.0053
```

Reading:

- Bayes alone breaks the frequentist guarantee whenever blocking is not much cheaper
  than a wrong approval: at `c_A:c_B = 1:1` it promotes 61% of boundary changes; at
  `1:10`, 98%. The composed rule stays at about 0.2%, below `alpha`.
- When `c_A >> c_B` (100:1, `alpha = 0.1`) the Bayes rule is stricter than the
  certificate and the composition matters: with 50,000 repetitions the composed rule's
  realised risk (0.587) is below the certificate's (0.594), as 4.2(3) predicts. With
  10,000 repetitions the realised ordering flipped; the paired standard error printed
  (0.0009) understates the uncertainty because the difference is driven by a handful of
  rare, 100-cost wrong approvals. We report both runs rather than pick one.
- When the certificate is the stricter of the two (most rows), the composed rule equals
  the certificate.

## What is known vs new

4.1 is textbook. 4.2 is a short argument we have not seen stated in this form for
promotion gates, but it is close to folklore (restricted Bayes, Hodges and Lehmann
1952, "The use of previous experience in reaching statistical decisions", Annals of
Mathematical Statistics). Literature check required.

## References

- J. O. Berger (1985). Statistical Decision Theory and Bayesian Analysis. Springer.
- J. L. Hodges, E. L. Lehmann (1952). The use of previous experience in reaching
  statistical decisions. Annals of Mathematical Statistics 23(3).
- P. Grünwald (2024). Beyond Neyman-Pearson: e-values enable hypothesis testing with a
  data-driven alpha. PNAS 121(39).
