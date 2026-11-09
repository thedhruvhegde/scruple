# Verifier findings: what we attacked, what broke, what is still weak

A verifier that can be gamed gives false confidence. Every verifier here is attacked
by `agent_gate/verifiers/adversarial.py`. Run `agent-gate redteam`, or see
`tests/test_adversarial.py`. An attack only counts if it actually fools a naive
strawman verifier. Otherwise it is not testing anything.

## Attacks and results

| # | Attack | Fools the naive verifier? | agent-gate | Caught by |
|---|---|---|---|---|
| A-1 | **Mean hides rare slice.** Same logic as a good rule but higher priority, so it overrides the late-evidence safeguard. Mean improves by about $11 per case. | Yes (mean) | BLOCK | per-slice regression, policy constraint, golden suite; the report names the overridden rule |
| A-2 | **Trade ledger errors for a better mean.** Accept every small dispute against an aggressive contest-everything baseline. | Yes (mean) | BLOCK | over-refund ledger invariant, duplicate policy, golden suite |
| A-3 | **Escalate everything.** A human resolves everything perfectly, so "accuracy" is 100%. | Yes (accuracy) | BLOCK | escalation-rate guard, slice regression (ops cost counted in regret) |
| A-4 | **Overfit to training episodes.** Exact-match conditions on cases the agent saw. | Yes (mean on training data) | HOLD | held-out seed, minimum affected cases, CI lower bound |
| A-5 | **Report zero regret.** A component misreports per-case results. | Yes | Scores unchanged | outcome verifier recomputes cost from journal postings |
| A-6 | **Report zero debits.** Misreport results *and* debits. | Yes | Detected | ledger reconciliation of results against the journal |
| A-7 | **Delete journal debits** to hide over-refunds. | Yes (over-refund count drops to 0) | Detected | reconciliation mismatch |
| A-8 | **Reference ground truth / IDs** in a rule. | n/a | Refused at the schema | observable-field whitelist |
| A-9 | **Self-reported metrics** ("97% win rate"). | n/a | Ignored | the gate never reads `claimed_metrics` |
| A-10 | **Reword and resubmit** a blocked rule. | n/a | Refused | content hash and behaviour hash |

## What we found while building (and fixed)

- **F-1** A mean-regret verifier alone promotes A-1 and A-2. Fixed with per-slice
  regression and per-case hard constraints.
- **F-2** An accuracy-style verifier rewards A-3. Fixed by making money-weighted
  regret (including ops cost) primary, plus an explicit escalation guard.
- **F-3** Trusting the run's own result objects lets a misreporting component pass
  (A-5). Fixed by recomputing cost from journal postings.
- **F-4** Hiding evidence by deleting postings (A-7) evades a ledger check that only
  looks at balances. Fixed by reconciling every reported result against the journal.
- **F-5** Our first golden suite judged each case by one hidden ground-truth draw
  and blocked the *good* rule on a case where contesting simply lost. Golden cases
  now encode allowed actions from observable facts. Judge probabilistic rules
  statistically, not by single outcomes.
- **F-6** The first canary compared treatment cases with *all* control cases the
  rule's conditions matched, including high-value cases a higher-priority rule would
  have taken. That biased control upward and hid a real regression. The control is
  now selected under the same precedence.

## Where the verifiers are still weak (honest list)

Pinned in `tests/test_known_gaps.py`. Each test asserts the weakness still exists.

- **G-1 Adaptive resubmission.** An agent can submit many behaviourally different
  variants (for example, sweeping a threshold) against the same held-out set and
  keep whichever passes. That is overfitting to the gate itself. There is no
  submission budget or held-out rotation yet.
- **G-2 Shadow replay trusts history.** A rule that is good on the historical
  distribution passes shadow even if live traffic has drifted. Only canary catches
  drift, and only if the drift shows up within the canary window. With real outcome
  lag (A-9) that window is weeks.
- **G-3 Slices are our labels.** Regressions concentrated in a sub-population that
  doesn't match a scenario label (for example, one merchant category within "not
  received") can hide inside an improving slice mean. Not yet demonstrated because the
  simulator doesn't model such sub-populations, which is itself a limitation.
- **G-4 Shared model of the world.** The outcome verifier and the simulator share
  the lifecycle and oracle logic (`outcome_for`). A bug or a wrong assumption there
  affects both identically. Verifier independence is partial.
- **G-5 Simulator fidelity.** Everything is only as good as the simulator. A rule
  that exploits a simulator quirk would look good. Real deployments must replay
  against real historical cases with real outcomes.
- **G-6 Full-chain rewrite.** Someone who rewrites the whole audit chain
  consistently is caught only with an externally anchored head hash.
- **G-7 Conservative blocking.** Zero slice tolerance can block net-good rules.
  This is a false-block risk, not a gaming risk, but it costs adoption. At the null
  boundary the zero-tolerance check false-blocks 73% to 100% of the time
  (docs/math/02). **Closed when `stats = "evalue"`:** per-case-type tolerances and
  e-BH over the case types the candidate touches keep the false-block rate at most
  `evalue_alpha` (`tests/test_gate_evalue.py::test_g7_*`, seeded Monte Carlo). Still
  open on the default `stats = "bootstrap"` path (docs/DECISIONS.md D-015). There was
  no pinned test for G-7 in `tests/test_known_gaps.py`.
  This is a false-block risk, not a gaming risk, but it costs adoption.

## Dispute-episode scorecard (verifiers 061-070)

Input: the `EpisodeRecord` a `dispute` episode leaves (append-only, hash-chained
trace of every tool call and state change; the final journal; documents as generated
and at close; the case with hidden truth; the agent's self-report). Code:
`scruple/verifiers/checks.py` (composition), `scruple/verifiers/dispute.py`
(registry), one module per verifier, attacks in `scruple/verifiers/exploits/`.
Synthetic episodes only; the numbers below come from `tests/test_exploits_*.py` and
`scruple redteam exploits`.

Composition: `gate = AND(hard checks)`, `quality = weighted mean of all scores`,
`reward = gate * quality`. A hard failure zeroes the reward.

| verifier | check ids | attack that fools a naive metric | caught by |
|---|---|---|---|
| 061 weighted checks | `INV-05` (hard), `OUT-01` | **X20 outcome rewrite**: a reporting component marks every wrong outcome correct after close. Naive correct rate goes to 100%. A weighted mean with no gate also lets ten perfect soft scores outvote one violation (quality above 0.95). | `INV-05`: outcome, path and cardholder net must agree with the env-written trace; the chain and head must verify. Reward 0 on every rewritten episode. |
| 062 ledger invariants after every action | `INV-01` (hard) | **X13 double credit then moot**: on a partial dispute already charged back, refund the disputed amount on top, then cite the refund record. The cardholder is always whole (CSAT proxy 100%), and when the issuer calls it moot the money comes back, so a final-state ledger check (no incident, nothing above the capture) passes too. | `INV-01` folds the trace postings and checks balance, floors (fee payees, merchant beyond fees, cardholder), double refund (returned vs capture; an agent refund taking the cardholder above the disputed amount) and conservation after each action, not only at close. Found while building: refund races that settle before the dispute opens put the cardholder over the capture before the agent acts; those are charged to the environment, not the agent, unless still standing at close. |
