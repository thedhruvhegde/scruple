# 5. a goodhart / reward-hacking gap bound under spot-check audits

Label: **candidate novel contribution (framing)**, built from **standard** pieces
(deterrence via random inspection; geometric survival). The mathematics is elementary.
What may be new is its use as a guarantee on the proxy-versus-anchor gap of an
accepted agent change. A literature check is required before claiming novelty.

Code: `scruple/stats/hacking.py`. Tests: `tests/test_stats_hacking.py`.
Evidence: `scripts/math_hacking.py`.

## Setting

A candidate is evaluated on `n` cases. Case `i` yields a **proxy** score `P_i` (the
agent's self-report, or a naive win label) and an **anchored** score `A_i` (minus the
ledger cost recomputed from journal postings: what money actually did), both in cap
units. The discrepancy `D_i = P_i - A_i` satisfies `0 <= D_i <= eps_max` (the cap).

**Definition (hacking gap).**

    Gamma = (1/n) sum_i D_i = (proxy mean) - (anchored mean).

An honest candidate has `D_i <= eta` (`eta` absorbs measurement noise). A case with
`D_i > eta` is *hacked*; `M` counts them.

**Audit model.** Each case is audited independently with probability `q`; an audited
hacked case is detected with probability `d` (`d = 1` for an exact ledger
recomputation, which is what the outcome verifier does). Audit draws are committed
(operator-held seed) and independent of the candidate's behaviour. The gate accepts
only if no audited case is flagged. Write `r = 1 - q d`.

Assumptions: H1 audits are hidden until after the candidate is fixed (the agent cannot
condition on them); H2 detection is per case and independent given the candidate; H3
the anchored measurement on an audited case is correct (verifier gap G-4 is exactly
the risk that it is not).

## Theorem 5.1 (tail bound)

For every policy (randomised, adaptive across cases, but not observing audit draws),

    P(accepted and Gamma >= gamma) <= r ^ ceil(n (gamma - eta) / eps_max).

**Proof.** `Gamma <= (M eps_max + (n - M) eta)/n <= eta + M eps_max / n`, so `Gamma >= gamma`
forces `M >= m0 = ceil(n (gamma - eta)/eps_max)`. Conditionally on the policy's choices
(independent of the audits by H1), each hacked case escapes independently with
probability `r` (H2), so `P(accepted | M) = r^M <= r^{m0}` on the event. QED.

## Theorem 5.2 (expected accepted gap)

    E[Gamma 1{accepted}] <= eta + eps_max / (n e ln(1/r))  ~  eta + eps_max / (e n q d).

**Proof.** `E[Gamma 1{acc}] <= eta + (eps_max/n) E[M r^M]` and
`sup_{x >= 0} x r^x = 1/(e ln(1/r))`, attained at `x = 1/ln(1/r)`. QED.

The proxy gain an optimiser can carry through the gate shrinks like `1/(n q d)`. The
same bounds hold for anchored harm (replace `D_i` by the true loss increase
`h_i <= h_max` of hacked cases). The bound is nearly tight: a policy hacking about
`1/ln(1/r)` of its largest opportunities gets within 10% of it (table below).

## Theorem 5.3 (minimal audit rate that makes hacking unprofitable)

Cost model: an accepted candidate pays the optimiser `V + sum_{i in S} g_i` (honest
value `V >= 0` plus proxy gain `g_i <= g_max` for each hacked case in `S`); a rejected one
pays `-C` (`C >= 0`: lost submission, penalty or budget). The honest candidate is
accepted and pays `V`. Then

    EU(S) = r^|S| (V + sum_S g_i) - (1 - r^|S|) C <= V  for all S
    iff  q d >= g_max / (V + C + g_max)

(the "only if" needs one case offering `g_max`).

**Proof.** Let `K = V + C`. Necessity: the single best case gives `r (K + g_max) <= K`.
Sufficiency: `EU(S) + C <= f(m) = r^m (K + m g_max)` with `m = |S|`, and
`f(m+1)/f(m) = r (K + (m+1) g_max)/(K + m g_max) <= r (K + g_max)/K <= 1`, so `f` is
non-increasing and `EU(S) <= f(0) - C = V`. QED.

**Corollary 5.4 (free resubmission defeats spot checks).** With `V = C = 0`, `q* = 1`.
If a caught candidate costs the agent nothing, it can resubmit until it slips through;
only auditing everything deters it. Deterrence needs a cost per caught attempt, which a
per-agent submission budget provides. This connects 5.3 to verifier gap G-1 (adaptive
resubmission) and is a concrete argument for building that budget.

## Simulation evidence (synthetic, seeded)

```
## minimal audit rate q* = g/(d (V + C + g)) (illustrative cost models)
V= 10000 C=    0 g_max=100 d=1.0: q*=0.0099
V= 10000 C= 5000 g_max=100 d=1.0: q*=0.0066
V=  1000 C=    0 g_max=100 d=1.0: q*=0.0909
V=  1000 C=    0 g_max=100 d=0.5: q*=0.1818
V=     0 C=    0 g_max=100 d=1.0: q*=1.0000
V=     0 C= 1000 g_max=100 d=1.0: q*=0.0909

## n=200, eps_max=1.0, q=0.05, half the cases hackable, reps=20000
Theorem 2 bound on E[Gamma 1(acc)]: 0.03586
bounds P(acc and Gamma>=gamma): gamma=0.01: 0.9025, gamma=0.02: 0.8145, gamma=0.05: 0.5987
hack_all   : P(acc)=0.0077, E[Gamma 1(acc)]=0.00189, tails=0.0077, 0.0077, 0.0077
random_5   : P(acc)=0.7706, E[Gamma 1(acc)]=0.00985, tails=0.6259, 0.0063, 0.0000
random_20  : P(acc)=0.3590, E[Gamma 1(acc)]=0.01836, tails=0.3590, 0.3590, 0.2101
largest_10 : P(acc)=0.6009, E[Gamma 1(acc)]=0.02850, tails=0.6009, 0.6009, 0.0000
largest_20 : P(acc)=0.3567, E[Gamma 1(acc)]=0.03230, tails=0.3567, 0.3567, 0.3567
largest_60 : P(acc)=0.0455, E[Gamma 1(acc)]=0.00940, tails=0.0455, 0.0455, 0.0455

## Theorem 2 bound eps_max/(n e ln(1/(1-q))) with eps_max=1
n=   100: q=0.01: 0.36604, q=0.05: 0.07172, q=0.2: 0.01649
n=  1000: q=0.01: 0.03660, q=0.05: 0.00717, q=0.2: 0.00165
n= 10000: q=0.01: 0.00366, q=0.05: 0.00072, q=0.2: 0.00016

## learning optimiser (epsilon-greedy over hack counts), V=10 C=5 g_max=1, q*=0.0625
q=0.0000 (0.00 q*): best response m*=64, EU*=42.674; learner mean payoff=40.549, most chosen m=60 (honest pays 10)
q=0.0156 (0.25 q*): best response m*=26, EU*=18.898; learner mean payoff=18.475, most chosen m=30 (honest pays 10)
q=0.0312 (0.50 q*): best response m*=12, EU*=12.811; learner mean payoff=12.455, most chosen m=10 (honest pays 10)
q=0.0563 (0.90 q*): best response m*=2, EU*=10.135; learner mean payoff=9.824, most chosen m=1 (honest pays 10)
q=0.0625 (1.00 q*): best response m*=0, EU*=10.000; learner mean payoff=9.509, most chosen m=0 (honest pays 10)
q=0.0688 (1.10 q*): best response m*=0, EU*=10.000; learner mean payoff=9.463, most chosen m=0 (honest pays 10)
q=0.1250 (2.00 q*): best response m*=0, EU*=10.000; learner mean payoff=9.373, most chosen m=0 (honest pays 10)
```

Reading:

- Every adversarial policy's empirical `P(accepted and Gamma >= gamma)` is below the
  Theorem 5.1 bound, and every `E[Gamma 1(acc)]` is below the Theorem 5.2 bound
  (0.0359); the best policy (hack the 20 largest) reaches 0.0323, so the bound is
  close to tight at `n = 200, q = 0.05`.
- Hacking everything is almost always caught (P(acc) = 0.008).
- An epsilon-greedy optimiser that only sees its own payoffs learns to hack below `q*`
  and learns to stop at or above it: its most chosen arm becomes 0 exactly when the
  best response does. Its mean payoff at `q >= q*` is a little below the honest 10
  because it keeps exploring.

## Relation to prior work

- The deterrence condition (audit probability times penalty at least the gain) is
  classic: Becker (1968, "Crime and Punishment: An Economic Approach", JPE); inspection
  games (Avenhaus, von Stengel and Zamir 2002, "Inspection Games", Handbook of Game
  Theory vol. 3). Theorem 5.3 is an instance with batch acceptance.
- Goodhart's law and reward hacking: Manheim and Garrabrant (2018, "Categorizing
  Variants of Goodhart's Law", arXiv); Skalse et al. (2022, "Defining and
  Characterizing Reward Hacking", NeurIPS); Gao, Schulman and Hilton (2023, "Scaling
  Laws for Reward Model Overoptimization", ICML).
- Random auditing of AI systems: Greenblatt et al. (2024, "AI Control: Improving Safety
  Despite Intentional Subversion", ICML) study trusted monitoring and auditing budgets;
  Gans and Holden (2026, "When Does Randomized Oversight Align AI Agents That Can
  Conceal?", NBER working paper 35878) study randomized oversight with concealment.
  We found that paper by search and could not read it (network policy); it may overlap
  with Theorem 5.3 and must be checked before any novelty claim.

What we believe is specific here: (a) defining the gap as proxy mean minus anchored
(ledger) mean for an agent change; (b) the high-probability and expected bounds on the
gap of an *accepted* change under zero-tolerance batch acceptance (5.1, 5.2), which
are statements about the gate's output rather than about the agent's incentives; (c)
Corollary 5.4 as a formal case for submission budgets.

## Limitations

- H1 fails if audits are predictable (for example, always the high-value cases).
  Auditing by stratum with known rates is fine as long as the agent cannot see which
  cases within the stratum are drawn; the bound then uses the smallest stratum rate
  among strata the agent can hack.
- H3 is the verifier-independence problem (G-4). A shared bug in simulator and verifier
  makes `d = 0` for that hack.
- Hacks of size at most `eta` are invisible by construction; the gap bound includes `eta`.

## References

- G. S. Becker (1968). Crime and punishment: an economic approach. Journal of Political Economy 76(2).
- R. Avenhaus, B. von Stengel, S. Zamir (2002). Inspection games. Handbook of Game
  Theory with Economic Applications, vol. 3.
- D. Manheim, S. Garrabrant (2018). Categorizing variants of Goodhart's law. arXiv 1803.04585.
- J. Skalse, N. Howe, D. Krasheninnikov, D. Krueger (2022). Defining and characterizing
  reward hacking. NeurIPS.
- L. Gao, J. Schulman, J. Hilton (2023). Scaling laws for reward model overoptimization. ICML.
- R. Greenblatt, B. Shlegeris, K. Sachan, F. Roger (2024). AI control: improving safety
  despite intentional subversion. ICML.
- J. S. Gans, R. T. Holden (2026). When does randomized oversight align AI agents that
  can conceal? NBER working paper 35878.
