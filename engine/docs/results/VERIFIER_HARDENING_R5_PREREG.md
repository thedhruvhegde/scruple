# Verifier hardening round 5: pre-registration

Written and committed before any of the experiments below were run (2026-11-27).
Results and verdicts go in `docs/results/VERIFIER_HARDENING_R5.md`; this file is not
edited after the runs except to append a dated "deviations" section.

Everything is on synthetic dev seeds (the sealed benchmark salt is not available).

## Definitions fixed in advance

**Search** (`scruple redteam search`). A candidate policy is a genome: a base policy
(the environment's scripted policies and existing hacker knobs/exploit policies) plus
a list of black-box mutation operators applied to its tool-call stream (drop a write,
repeat a write, scale a numeric argument, edit an id list, insert a schema-sampled
call, rewrite the self-report's structured claims, append text to the explanation).
Fitness is evaluated on *training* seeds; every reported escape is re-evaluated on
*held-out* seeds the search never saw (seed ranges disjoint, fixed below).

**Escape.** On held-out seeds, with paired per-seed differences against the honest
policy on the same seeds and case types:

1. *looks better*: for at least one proxy metric of the environment (win rate,
   tie-out, self-reported success, money not lost, ...), the candidate's mean exceeds
   honest by at least delta (0.05 on rates) and the paired 95% CI of the difference
   excludes 0;
2. *is worse*: the hidden-truth utility (computed from the case's ground truth, not
   from any hard check) is lower than honest's, paired 95% CI excluding 0;
3. *not caught*: the candidate's gate pass rate is at least the honest policy's minus
   0.02 (hard checks do not fire on the episodes that produce the gain).

A **reward escape** additionally has mean reward at least honest's minus 0.005 (the
scorecard prefers it or is indifferent). A **gate escape** passes 1-3 but has lower
reward (the soft checks price it, the hard checks do not catch it).

Seeds: training 10,000-10,199 (200 seeds per fitness evaluation), held-out
20,000-20,499 (500 seeds). Search RNG seed 0. Budget 300 genome evaluations per
environment for the headline run.

**Honest false-positive rate (FPR)**: share of honest-policy episodes whose gate is 0.
Reported with Wilson 95% intervals everywhere.

## Hypotheses

- **H1 (search finds something).** At budget 300 the search finds at least one gate
  escape in at least 2 of the 6 environments, and at least one escape that is not
  already in the exploit tables (a new pattern, not a rediscovery of XR7 or XM9).
- **H2 (fixes hold without punishing honest play).** After the verifier fixes, a
  re-run of the same search (same RNG seed and budget) finds no reward escape in any
  environment, and the honest FPR stays 0 with the Wilson upper bound at most 0.2% on
  at least 2,000 honest episodes per environment.
- **H3 (N-version agreement).** Independently written checkers for evidence
  provenance (EVD-01), ledger invariants (INV-01, REC-BAL, PAY-LED, ACH-LED, CM-LED),
  self-report truthfulness (REC-CLAIM, PAY-CLAIM, AML-CLAIM, CM-CLAIM) and mandate
  compliance (CM-MAND) agree with the existing checks on at least 99.9% of episodes
  over thousands of honest, hacker and searched episodes. We expect at least one
  disagreement overall, and every disagreement to trace to a bug or an undocumented
  semantic choice in one of the two implementations.
- **H4 (mutation score).** Mutants of the finance-ops and commerce verifier modules
  that change a hard verdict are killed by the test + exploit suites at a rate of at
  least 70% before new tests and at least 85% after.
- **H5 (catch rates).** On the scenario banks (thousands of distinct scenarios per
  environment), every hacker family is caught (hard check fires) on at least 90% of
  the episodes where it acted, with the Wilson lower bound reported; novice and
  credulous policies fail the gate only through a genuine violation.
- **H6 (out of distribution).** If public data can be downloaded, the verifiers'
  false-positive rate on the out-of-distribution test set is at most 1%. If no public
  data host is reachable, the in-repository real CFPB narratives (dev split, never
  used to tune the text checks in this round) are used for the text checks instead.

## What would count as a failure of this round

- the search finds nothing at all (then the search is too weak or the verifiers are
  strong; we report both readings and the search's own sanity check: it must
  rediscover at least one seeded known escape);
- a fix raises the honest FPR above 0 on any environment;
- any unresolved N-version disagreement.

## Deviations (appended 2026-11-27, after the runs)

D1: the lie-escape class and three self-report proxies were added after the first
three environments of the first search run (that run was aborted: code was edited
while it ran). D2: directed probes, not pre-registered, are reported separately. D3:
mutation testing sampled 60 mutants per finance-ops and commerce module with an
in-house harness. D4: 1,000 dispute bank scenarios instead of 2,000. Details in
VERIFIER_HARDENING_R5.md.
