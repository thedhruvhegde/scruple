# 5. a goodhart / reward-hacking gap bound under spot-check audits

Label: **candidate novel contribution (framing)**, built from **standard** pieces
(5.5 to 5.8 below extend it to adaptive agents with leaky audits and an imperfect verifier)
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

## Extensions: adaptive agents, leaky audits, an imperfect verifier (Theorems 5.5 to 5.8)

Label: **adaptation** (5.5, 5.6: a supermartingale version of the survival argument)
and **candidate novel (framing)** (5.7, 5.8: the deterrence threshold with both
verifier error rates). Code: `scruple/stats/hacking_general.py`. Tests:
`tests/test_stats_hacking_general.py`. Evidence: `scripts/math_hacking_general.py`.

Theorems 5.1 to 5.3 assume audits and detections independent of each other and of
the policy (H1, H2). Real gates break both: an agent may learn something about which
cases get audited (high amounts, a reviewer's queue), and the verifier misses some
hacks (false-negative rate `beta`) and flags some honest cases (false-alarm rate `phi`;
the honest reference fails INV-01 on 19 of 10,000 benchmark cases, docs/experiments).

**Sequential model.** Cases are handled one at a time. Before case `i` the agent holds
information `G_i`: the history `F_{i-1}`, whatever signal it receives about case `i`,
and its own randomness. It chooses `D_i in [0, eps_max]` (a function of `G_i`). Let
`h_i = 1{D_i > eta}`, `M_t = sum_{i <= t} h_i`, and `Fl_i` the event that case `i` is
flagged. The batch is accepted iff no case is flagged. Nothing is assumed about the
joint law of audits, flags and choices except

> (R) on `{h_i = 1}`: `P(Fl_i | G_i, D_i) >= rho` for a constant `rho in [0, 1]`.

(R) allows randomised and adaptive policies, audits that depend on the past, a policy
that sees past audit results, and correlated detections. Flags on honest cases are
allowed and only lower acceptance.

### Theorem 5.5 (tail and expectation under (R))

For every policy satisfying (R), with `m0 = ceil(n (gamma - eta)/eps_max)`,

    P(accepted and Gamma >= gamma) <= (1 - rho)^m0,
    E[Gamma 1{accepted}] <= eta + eps_max / (n e ln(1/(1 - rho))).

**Proof.** Let `A_t` be "no flag among the first `t` cases" and
`Z_t = 1{A_t} (1 - rho)^(-M_t)`, `Z_0 = 1`. With `H_i = sigma(G_i, D_i)` (which contains
`F_{i-1}` and decides `h_i`),

    E[Z_i | H_i] = Z_{i-1} (1 - rho)^(-h_i) P(not Fl_i | H_i) <= Z_{i-1},

because if `h_i = 1` then `P(not Fl_i | H_i) <= 1 - rho` by (R), and if `h_i = 0` the
factor is at most 1. By the tower property `E[Z_n] <= E[Z_0] = 1`. On acceptance
`Z_n = (1 - rho)^(-M_n)`, so

    P(A_n and M_n >= m0) = E[Z_n (1 - rho)^(M_n) 1{M_n >= m0}] <= (1 - rho)^m0 E[Z_n],

and `Gamma >= gamma` forces `M_n >= m0` as in 5.1. For the expectation,
`E[M_n 1{A_n}] = E[Z_n M_n (1 - rho)^(M_n)] <= sup_x x (1 - rho)^x = 1/(e ln(1/(1 - rho)))`,
and `Gamma <= eta + eps_max M_n / n`. QED.

With independent audits at rate `q` and detection `1 - beta`, `rho = q (1 - beta)` and
5.5 is 5.1 and 5.2 with `d = 1 - beta`; the new content is that independence is not
needed. With strata audited at different rates, `rho` is the smallest
`q_k (1 - beta_k)` over strata the agent can hack.

### Corollary 5.6 (leaky audits)

Let case `i` be audited (`a_i = 1`) with probability `q`, independently of `F_{i-1}`, and
let the agent observe a signal `s_i` with `P(s | a = 1) >= l P(s | a = 0)` for every `s`,
for some `l in [0, 1]`; given an audit, a hacked case is flagged with probability at
least `1 - beta` whatever the signal. Then (R) holds with

    rho = (1 - beta) q l / (q l + 1 - q).

**Proof.** By Bayes, `P(a = 1 | s, F_{i-1}) = q L / (q L + 1 - q)` with
`L = P(s | a = 1)/P(s | a = 0) >= l`, and `x -> q x/(q x + 1 - q)` is increasing. Choices
depend on `(F_{i-1}, s_i)` and independent randomness only, so
`P(Fl_i | G_i, D_i) >= (1 - beta) P(a_i = 1 | G_i) >= rho`. QED.

`l = 1` is a blind agent; `l = 0` an agent that may see the audits, for which no bound
is possible (`rho = 0`). The bound is attained: an agent that sees "audited" with
probability `1 - l` on audited cases and hacks only otherwise has catch probability
exactly `rho` on each hacked case (table below, ratio 1.000).

### Theorem 5.7 (minimal audit rate with false negatives and false alarms)

Independent audits at rate `q`; an audited hacked case is flagged with probability
`1 - beta`, an audited honest case with probability `phi`, independently. Payoffs as in
5.3 (`V`, `C`, gains `g_i <= g_max`, `K = V + C > 0`), and the honest candidate is now
accepted only with probability `(1 - q phi)^n`. Assume `q phi < 1`. Then hacking any set
`S` is no better than honesty, `EU(S) <= EU(empty)`, for all `S` iff

    q >= q*(beta, phi) = g_max / ((1 - beta)(K + g_max) - phi K)

(when the denominator is positive; "only if" needs one case offering `g_max`).

**Proof.** With `|S| = m`, `P(acc) = (1 - q phi)^(n - m) (1 - q (1 - beta))^m`, so
`EU(S) + C <= (1 - q phi)^n f(m)` with `f(m) = s^m (K + m g_max)` and
`s = (1 - q(1 - beta))/(1 - q phi)`, with equality when all gains equal `g_max`.
`f(m+1)/f(m) = s (K + (m+1) g_max)/(K + m g_max) <= s (K + g_max)/K`, so `f` is
non-increasing iff (for the worst `m = 0` step) `s (K + g_max) <= K`, i.e.
`(1 - q(1 - beta))(K + g_max) <= K (1 - q phi)`, i.e.
`q [(1 - beta)(K + g_max) - phi K] >= g_max`. Necessity is the `m = 1` case. QED.

**Corollary 5.8 (when no audit rate deters).** `q*(beta, phi) <= 1` iff
`beta g_max <= (1 - beta - phi) K`. Otherwise every `q in [0, 1]` leaves some hack
profitable. `q*` increases in both `beta` and `phi`: a false alarm costs the honest
candidate as much as a caught hack costs the hacker, so it shrinks the margin that
deters. It does not depend on `n`, but the honest acceptance probability
`(1 - q phi)^n` does: at `n = 10,000`, `q = 1` and `phi = 0.0019` it is about `5.5e-9`,
which is the INV-01 problem of E1 in one number.

### Evidence on the E2 policies (synthetic, seeded, exact where possible)

`uv run python scripts/math_hacking_general.py` (seconds). The hacked counts are the
120 (policy, batch) pairs E2 recorded (12 policies visited by the proxy climb, 10
batches of 200 benchmark cases); `beta`, `l` and `q` are swept (ASSUMPTION), and
`phi = 0.0019` is the honest policy's measured INV-01 rate on v1. The leak-aware
policy hacks the same cases as the E2 policy except where its signal says "audited".
Probabilities are computed exactly (binomial sums), and checked once against the
seeded simulator.

```
## E2 batches: 120 (policy, batch) pairs of n=200; 110 with hacks
hacked counts: min 24, max 149
leak-aware policy hacks only when its signal says 'not audited'; exact probabilities
rho = (1-beta) q l/(q l + 1 - q) (Theorem 5.5); naive rho = (1-beta) q ignores the leak

q=0.05 beta=0.0 l=1.0 phi=0.0   : rho=0.0500; max E[Gamma 1(acc)]=0.03504 vs bound 0.03586 (ratio 0.977; naive bound 0.03586); tail holds 440/440 (max ratio 0.815), naive tail violated 0/440
q=0.05 beta=0.0 l=1.0 phi=0.0019: rho=0.0500; max E[Gamma 1(acc)]=0.03446 vs bound 0.03586 (ratio 0.961; naive bound 0.03586); tail holds 440/440 (max ratio 0.801), naive tail violated 0/440
q=0.05 beta=0.0 l=0.5 phi=0.0   : rho=0.0256; max E[Gamma 1(acc)]=0.07078 vs bound 0.07081 (ratio 1.000; naive bound 0.03586 VIOLATED); tail holds 440/440 (max ratio 0.915), naive tail violated 21/440
q=0.05 beta=0.0 l=0.5 phi=0.0019: rho=0.0256; max E[Gamma 1(acc)]=0.06958 vs bound 0.07081 (ratio 0.983; naive bound 0.03586 VIOLATED); tail holds 440/440 (max ratio 0.899), naive tail violated 21/440
q=0.05 beta=0.0 l=0.2 phi=0.0   : rho=0.0104; max E[Gamma 1(acc)]=0.17557 vs bound 0.17566 (ratio 0.999; naive bound 0.03586 VIOLATED); tail holds 440/440 (max ratio 0.966), naive tail violated 79/440
q=0.05 beta=0.0 l=0.2 phi=0.0019: rho=0.0104; max E[Gamma 1(acc)]=0.17260 vs bound 0.17566 (ratio 0.983; naive bound 0.03586 VIOLATED); tail holds 440/440 (max ratio 0.949), naive tail violated 73/440
q=0.05 beta=0.3 l=1.0 phi=0.0   : rho=0.0350; max E[Gamma 1(acc)]=0.05103 vs bound 0.05163 (ratio 0.988; naive bound 0.05163); tail holds 440/440 (max ratio 0.867), naive tail violated 0/440
q=0.05 beta=0.3 l=1.0 phi=0.0019: rho=0.0350; max E[Gamma 1(acc)]=0.05019 vs bound 0.05163 (ratio 0.972; naive bound 0.05163); tail holds 440/440 (max ratio 0.853), naive tail violated 0/440
q=0.05 beta=0.3 l=0.5 phi=0.0   : rho=0.0179; max E[Gamma 1(acc)]=0.10153 vs bound 0.10156 (ratio 1.000; naive bound 0.05163 VIOLATED); tail holds 440/440 (max ratio 0.940), naive tail violated 21/440
q=0.05 beta=0.3 l=0.5 phi=0.0019: rho=0.0179; max E[Gamma 1(acc)]=0.09989 vs bound 0.10156 (ratio 0.984; naive bound 0.05163 VIOLATED); tail holds 440/440 (max ratio 0.923), naive tail violated 21/440
q=0.05 beta=0.3 l=0.2 phi=0.0   : rho=0.0073; max E[Gamma 1(acc)]=0.25129 vs bound 0.25134 (ratio 1.000; naive bound 0.05163 VIOLATED); tail holds 440/440 (max ratio 0.976), naive tail violated 73/440
q=0.05 beta=0.3 l=0.2 phi=0.0019: rho=0.0073; max E[Gamma 1(acc)]=0.24722 vs bound 0.25134 (ratio 0.984; naive bound 0.05163 VIOLATED); tail holds 440/440 (max ratio 0.958), naive tail violated 73/440
q=0.2  beta=0.0 l=1.0 phi=0.0   : rho=0.2000; max E[Gamma 1(acc)]=0.00057 vs bound 0.00824 (ratio 0.069; naive bound 0.00824); tail holds 440/440 (max ratio 0.410), naive tail violated 0/440
q=0.2  beta=0.0 l=1.0 phi=0.0019: rho=0.2000; max E[Gamma 1(acc)]=0.00053 vs bound 0.00824 (ratio 0.064; naive bound 0.00824); tail holds 440/440 (max ratio 0.383), naive tail violated 0/440
q=0.2  beta=0.0 l=0.5 phi=0.0   : rho=0.1111; max E[Gamma 1(acc)]=0.00851 vs bound 0.01562 (ratio 0.545; naive bound 0.00824 VIOLATED); tail holds 440/440 (max ratio 0.740), naive tail violated 27/440
q=0.2  beta=0.0 l=0.5 phi=0.0019: rho=0.1111; max E[Gamma 1(acc)]=0.00792 vs bound 0.01562 (ratio 0.507; naive bound 0.00824); tail holds 440/440 (max ratio 0.689), naive tail violated 27/440
q=0.2  beta=0.0 l=0.2 phi=0.0   : rho=0.0476; max E[Gamma 1(acc)]=0.03754 vs bound 0.03770 (ratio 0.996; naive bound 0.00824 VIOLATED); tail holds 440/440 (max ratio 0.627), naive tail violated 97/440
q=0.2  beta=0.0 l=0.2 phi=0.0019: rho=0.0476; max E[Gamma 1(acc)]=0.03486 vs bound 0.03770 (ratio 0.925; naive bound 0.00824 VIOLATED); tail holds 440/440 (max ratio 0.583), naive tail violated 85/440
q=0.2  beta=0.3 l=1.0 phi=0.0   : rho=0.1400; max E[Gamma 1(acc)]=0.00321 vs bound 0.01220 (ratio 0.264; naive bound 0.01220); tail holds 440/440 (max ratio 0.547), naive tail violated 0/440
q=0.2  beta=0.3 l=1.0 phi=0.0019: rho=0.1400; max E[Gamma 1(acc)]=0.00301 vs bound 0.01220 (ratio 0.247; naive bound 0.01220); tail holds 440/440 (max ratio 0.512), naive tail violated 0/440
q=0.2  beta=0.3 l=0.5 phi=0.0   : rho=0.0778; max E[Gamma 1(acc)]=0.01877 vs bound 0.02272 (ratio 0.826; naive bound 0.01220 VIOLATED); tail holds 440/440 (max ratio 0.789), naive tail violated 27/440
q=0.2  beta=0.3 l=0.5 phi=0.0019: rho=0.0778; max E[Gamma 1(acc)]=0.01747 vs bound 0.02272 (ratio 0.769; naive bound 0.01220 VIOLATED); tail holds 440/440 (max ratio 0.735), naive tail violated 21/440
q=0.2  beta=0.3 l=0.2 phi=0.0   : rho=0.0333; max E[Gamma 1(acc)]=0.05409 vs bound 0.05426 (ratio 0.997; naive bound 0.01220 VIOLATED); tail holds 440/440 (max ratio 0.710), naive tail violated 85/440
q=0.2  beta=0.3 l=0.2 phi=0.0019: rho=0.0333; max E[Gamma 1(acc)]=0.05027 vs bound 0.05426 (ratio 0.927; naive bound 0.01220 VIOLATED); tail holds 440/440 (max ratio 0.659), naive tail violated 82/440

## simulator vs exact (m=109, q=0.05, beta=0.3, l=0.5, phi=0.0019, reps=20000)
P(acc): simulated 0.1454, exact 0.1439 (SE 0.0025); E[Gamma 1(acc)]: simulated 0.07718, exact 0.07646

## minimal audit rate on the E2 proxy (Theorem 5.7)
proxy = mean(engine win rate, self-reported win rate); a false win claim adds g = 1/(2n) = 0.0025; honest value V = 0.449 (honest engine win rate on v1); up to 109 hackable cases per batch (claim_rate = 1)
C=0.0   beta=0.0  phi=0.0   : q*=0.00554; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.0   beta=0.0  phi=0.0019: q*=0.00555; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.0   beta=0.0  phi=0.05  : q*=0.00583; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.0   beta=0.25 phi=0.0   : q*=0.00738; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.0   beta=0.25 phi=0.0019: q*=0.00740; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.0   beta=0.25 phi=0.05  : q*=0.00791; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.0   beta=0.5  phi=0.0   : q*=0.01107; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.0   beta=0.5  phi=0.0019: q*=0.01112; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.0   beta=0.5  phi=0.05  : q*=0.01230; best response 0.5q* -> m*=109, 0.99q* -> m*=2, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.0  phi=0.0   : q*=0.00278; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.0  phi=0.0019: q*=0.00278; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.0  phi=0.05  : q*=0.00292; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.25 phi=0.0   : q*=0.00370; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.25 phi=0.0019: q*=0.00371; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.25 phi=0.05  : q*=0.00397; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.5  phi=0.0   : q*=0.00555; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.5  phi=0.0019: q*=0.00557; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0
C=0.449 beta=0.5  phi=0.05  : q*=0.00617; best response 0.5q* -> m*=109, 0.99q* -> m*=4, 1.01q* -> m*=0, 2.0q* -> m*=0

## no audit rate deters (Corollary 5.8): beta g > (1 - beta - phi) K
V=1.0 C=0.0 g=1.0 beta=0.5 phi=0.0: q*=1.000, deterrable=True
V=1.0 C=0.0 g=1.0 beta=0.6 phi=0.0: q*=1.250, deterrable=False
V=1.0 C=0.0 g=0.1 beta=0.5 phi=0.5: q*=2.000, deterrable=False
V=1.0 C=1.0 g=0.1 beta=0.0 phi=0.95: q*=0.500, deterrable=True

## learning optimiser with an imperfect verifier (V=10.0, C=5.0, g=1.0, n=200)
beta=0.0 phi=0.0: q*=0.0625; 0.5q*: m*=16, learner arm 20 (pays 13.22); 1.0q*: m*=0, learner arm 0 (pays 9.66); 2.0q*: m*=0, learner arm 0 (pays 9.42)
beta=0.3 phi=0.0: q*=0.0893; 0.5q*: m*=16, learner arm 5 (pays 12.15); 1.0q*: m*=0, learner arm 0 (pays 9.60); 2.0q*: m*=0, learner arm 0 (pays 9.40)
beta=0.3 phi=0.0019: q*=0.0895; 0.5q*: m*=17, learner arm 20 (pays 13.04); 1.0q*: m*=0, learner arm 0 (pays 9.12); 2.0q*: m*=0, learner arm 0 (pays 8.15)
beta=0.3 phi=0.01: q*=0.0905; 0.5q*: m*=17, learner arm 20 (pays 11.18); 1.0q*: m*=0, learner arm 1 (pays 7.25); 2.0q*: m*=0, learner arm 0 (pays 4.86)
```

Reading:

- Every exact tail and expectation sits under the Theorem 5.5 bound (440 of 440
  tail checks per row). With leakage the bound is attained to three decimals
  (ratio 1.000 at `q = 0.05`), so it cannot be improved without more assumptions.
- The bound that ignores leakage (`rho = q (1 - beta)`) is violated by the leak-aware
  policy in every row with `l < 1` at `q = 0.05`: an agent that learns where audits
  fall carries up to 0.25 of proxy gap per case through a gate whose naive bound
  promises 0.05.
- On the E2 proxy a false claim is worth `g = 1/(2n)`, so the deterrent rate is small
  (`q* = 0.0055` with a perfect verifier, `0.011` at `beta = 0.5`); the best response
  flips from hacking all 109 hackable cases to none between `0.99 q*` and `1.01 q*`, as
  the theorem says. False alarms at the measured rate move `q*` in the fourth decimal.
- The epsilon-greedy optimiser that sees only its payoffs stops hacking at `2 q*` in
  every row and at `q*` in all but one (at `phi = 0.01` it kept 1 hack; at exactly `q*`
  one hack ties with honesty). Its payoff at `2 q*` falls from 9.40 to 4.86 as `phi` goes
  from 0 to 0.01: false alarms tax honest submissions, which is a cost to adoption,
  not to safety.

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
