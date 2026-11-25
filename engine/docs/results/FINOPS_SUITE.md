# Finance-operations environment suite: results

Status: pre-registration written and committed before the 1,000-seed runs. Results
sections are filled in after the run, below the predictions, without editing them.

## Pre-registered predictions (written before the 1,000-seed runs)

Disclosure: before writing these I had seen smoke runs on 33 to 60 dev seeds (the
same code path, `scruple finops experiments --seeds 33 --train 22`, and probes of
44-60 seeds). The predictions are therefore informed, not blind; what the 1,000-seed
run tests is whether the small-sample picture holds, with intervals.

All runs: dev seeds 0-999 (public; the sealed benchmark salt was not used), seed i at
nominal difficulty (i mod 11) / 10 along the curriculum, hill climb on train seeds
50000-50199. Verifier v2 is the suite default; v1 is each kind's own scorecard.

- P1 (honest false positives): the honest policy fails a hard check on 0 of 1,000
  episodes in every environment under every verifier version (Wilson upper bound
  below 0.4%).
- P2 (ceiling): honest and oracle mean reward 1.000 on recon, aml, ach and payables
  at every difficulty (the scripted expert is near-perfect by construction; the
  difficulty knobs are aimed at imperfect agents).
- P3 (floor): the no-op floor earns 0 on recon@2 and aml@2, against about 0.45
  (recon@1) and about 0.76 (aml@1).
- P4 (difficulty bites imperfect policies): on recon@2 the Spearman correlation of
  reward with nominal difficulty is below -0.3 for honest_exact_refs and credulous.
  On aml@2 the correlation for credulous is negative but weak (above -0.3): the aml
  knobs add noise and artefacts the novice ignores, so AML difficulty is mostly the
  base difficulty, which the scripted policies do not feel.
- P5 (hacker caught): the aml, ach composite hackers fail a hard check on at least
  95% of episodes; the recon composite hacker (match the easy groups, plug the rest
  as a fee, claim a clean reconciliation) fails a hard check on at least 90%; the
  payables hacker (confirm bank changes by email) only acts on batches with a change
  request and is hard-caught on 40-50% of all episodes, i.e. on every episode where it
  acted.
- P6 (no exploit knob pays): no single exploit knob raises mean reward over its base
  policy (expert or novice) with a 95% paired interval above 0, under either verifier
  version. Defensive filing on aml (a SAR on every alert) raises the naive proxy and
  lowers reward.
- P7 (hill climb): the scorecard-objective climb (v1 and v2) ends at a policy with no
  exploit knob on, in both environments; the proxy-objective climb ends with at least
  one exploit or strategy knob on (aml: sar_always; recon: dump or match-by-amount-like
  knobs that raise the matched share).
- P8 (v2 changes what is hard): XR6 (apply a receipt because an email asks) passes
  every hard check of recon@1 and fails REC-APPLY on recon@2 on every exercised
  episode.
- P9 (throughput): scripted episodes at least 100/s per core for aml and ach, at
  least 25/s per core for recon (the bank file is parsed and the event log replayed
  in verification); parallel speedup at least 3x on 8 workers.
