# E1: do the verifiers and the gate catch planted regressions?

Status: **pre-registered** (this section is committed before the experiment runs on
benchmark v1; results are appended below in a separate commit and nothing above the
results heading changes after that commit).

Synthetic episodes only. Benchmark: v1, manifest hash
`1286baf3be84b4f9aa7af0606f4434897cf97db4a11c3db548d9549d2cb13f91` (10,000 held-out
dispute cases, docs/experiments/README.md).

## Question and hypothesis

When an otherwise good dispute policy acquires a narrow regression (a bug that hurts
one slice of cases), does scruple flag it, and does it leave changed-but-not-worse
policies alone?

- **H1** Every planted regression is flagged on the full benchmark.
- **H2** On the 500-case folds where a regression actually changed at least one
  episode, it is flagged on at least 80% of them (pooled over regressions).
- **H3** Benign controls are flagged on at most 5% of folds.

## Design

Baseline: the reference `evidence_policy` with a truthful self-report (C1 below is
that policy; its run is the baseline every other run is paired with).

Planted regressions (K = 7), each the evidence policy with one change
(`scruple/bench/plants.py`). Which cases are hit is chosen from observable data only
(category, reason code, amount, a hash of the episode id):

| id | regression | what a reviewer would call it |
|---|---|---|
| R1 | on 5% of `processing_error` disputes (a rare category), wait past the response deadline before acting | misses deadlines on a slice of a rare family |
| R2 | on 2% of all disputes, wait past the deadline | queue stall |
| R3 | on Visa 13.1 (merchandise not received), cite the documents the code does *not* ask for instead of the required ones | wrong evidence on one reason code |
| R4 | below $25, refund the captured amount instead of the disputed amount | over-refunds small amounts |
| R5 | below $25, concede every dispute even when the evidence wins | gives away small disputes |
| R6 | never request the cardholder's statement | skips an investigation step |
| R7 | report every representment as won, whatever happened | overclaims in its report |

Controls (C2-C4 count toward the false-alarm rate; C1 is a sanity check, identical
to the baseline by construction, so it must never be flagged):

| id | control |
|---|---|
| C1 | unmodified |
| C2 | reads the documents and cites them in reverse order |
| C3 | waits one day before acting when at least a week is left |
| C4 | states no win probability |

Detector (`scruple/bench/detect.py`), candidate against baseline on the same cases,
flagged if any of:

1. **verifier**: some episode fails a hard check the baseline passes on that case;
2. **batch**: a batch check (CON-01, CAL-01, SR-03) fails on a block of 500 where
   the baseline passes;
3. **gate**: `scruple.stats.evaluate_pairs` returns `block` on the paired merchant
   costs, case type = case family, cost cap $200 per case (differences clipped to
   +-$200; ASSUMPTION: clipping only shrinks a regression), `alpha_block = 0.05`,
   zero tolerance per type, `shuffle_seed = 0`.

Units of analysis: the full benchmark (one trial per policy) and its 20 disjoint
blocks of 500 consecutive cases (the folds; 20 trials per policy). A fold is
*exercised* by a candidate if at least one episode differs from the baseline's in
cost, failed checks, tool-call count, outcome or reward.

## Metrics and pass bar

- P1 (H1): all 7 regressions flagged on the full benchmark.
- P2 (H2): flagged exercised folds / exercised folds, pooled over R1-R7, >= 0.80.
  Reported with a Wilson 95% interval and per regression.
- P3 (H3): flagged folds / (3 controls x 20 folds) <= 0.05. Reported with a Wilson
  95% interval; the full-benchmark flags of C2-C4 are reported too.
- Sanity: C1 never flagged.

**E1 passes if and only if P1, P2 and P3 all hold.** Reported either way.

Secondary (reported, no pass bar): which detector fired for each regression; the
gate's verdict per regression on the full benchmark; mean paired cost difference.

## Sample size (scruple.stats planning)

`uv run python scripts/exp_planted.py plan` (reads only the published manifest):

```
benchmark v1 family shares (from the published manifest, n=10000):
  buyers_remorse       0.1977 (1977 cases)
  cardholder_mistake   0.0577 (577 cases)
  friendly_fraud       0.2791 (2791 cases)
  merchant_error       0.0625 (625 cases)
  service_failure      0.1840 (1840 cases)
  true_fraud           0.2190 (2190 cases)

gate cost cap 20000 USD minor; alpha 0.05, power 0.8 (scruple.stats.plan)
  regression   500 per case, sd   2000:  1058 cases per family,  19246 held-out cases in total (binding: cardholder_mistake)
  regression  2000 per case, sd   6000:   422 cases per family,   7892 held-out cases in total (binding: cardholder_mistake)
  regression  5000 per case, sd  10000:   195 cases per family,   3775 held-out cases in total (binding: cardholder_mistake)

fold size 500: a hard-check regression hitting a share f of cases is exercised
in a fold with probability 1 - (1 - f)^500:
  f = 0.001  P(exercised) = 0.394
  f = 0.002  P(exercised) = 0.632
  f = 0.005  P(exercised) = 0.918
  f = 0.01   P(exercised) = 0.993
  f = 0.05   P(exercised) = 1.000
```

Reading: at 10,000 cases the statistical gate alone is powered (0.8) for a per-family
cost regression of about $20 per case with a $60 spread, not for $5 (that needs about
19,000). In a 500-case fold it is powered only for regressions of about $50 per case
in every family. So the gate is expected to carry little of the fold-level detection;
the hard checks must. A regression that hits 0.1% of cases is exercised in only about
40% of folds, which is why H2 conditions on exercised folds. The sample is the whole
frozen benchmark: 10,000 cases, 20 folds, 7 x 20 regression-fold trials and
3 x 20 control-fold trials. With 60 control trials the Wilson upper bound on a
zero false-alarm count is about 6%, so P3 is a point-estimate bar, not a proof of a
rate below 5%.

## Predictions written down before the run

- R6 (PROC-01) and R7 (SR-01) are caught on every exercised fold: they change most
  episodes and a hard check reads exactly that.
- R1, R2 are caught by DL-01 wherever exercised; R1 is exercised in few folds.
- R4 fires DL-01 / INV-01 / SCOPE-01 (an over-refund is refused or breaks the
  ledger, and the stalled case lapses).
- R3 and R5 break no hard rule; only the gate can see them. They are the most
  likely to be missed, both on folds and possibly on the full benchmark.
- C2-C4 fire nothing.

## Analysis plan

`SCRUPLE_HELDOUT_SALT=<v1 salt> uv run python scripts/exp_planted.py run --jobs 4`
plays the 12 policies on all 10,000 cases, writes
`results/experiments/E1_planted_regressions.json` (per policy: full and per-fold
detections, which detector and check fired, the gate verdict, mean cost difference)
and prints the P1-P3 verdict. No re-runs with different settings, no dropping of
policies or folds; any deviation from this plan is reported as a deviation.

Disclosure: before this pre-registration the policies and the detector were
smoke-tested on 1,000 cases from a throwaway salt (no benchmark-v1 case was played).
That test changed one thing: the first version of R3 (cite every listed document on
13.1) changed no cost and no hard check, so it was not a regression; R3 now cites the
wrong kinds. The cost cap was set to $200 after the planning output above showed a
$500 cap needs about 31,500 cases for a $5 regression.

## Results

> **Correction (2026-11-10), units.** The dollar amounts in this Results section are
> USD minor units (cents) mislabelled as dollars. Read "$188" as $1.88 per case, "$60"
> as $0.60, and "$1.30" as $0.013 (1.3 cents). The verdicts, detection counts, e-values
> and pass or fail calls do not change, because they never depended on the units. The
> sample-size section above (written before the run) used planning inputs, not these
> results, and is left as registered. The paper (docs/paper/scruple_paper.pdf) reports
> the corrected values. It also shows that a wider, still valid bet detects R5 on this
> benchmark, so the miss came mostly from the betting cap, not from a power limit.

Run on 2026-11-09, one run as planned, no deviations:
`scripts/exp_planted.py run --jobs 4` with the v1 salt (956 s on a shared 4-core
container). Raw output: `results/experiments/E1_planted_regressions.json`.
Synthetic episodes only.

**Verdict: PASS, narrowly.** P1 holds; P2 and P3 meet their point-estimate bars but
neither is established by its interval.

| pre-registered | result | bar | holds |
|---|---|---|---|
| P1 all regressions flagged on the full benchmark | 7 of 7 | 7 of 7 | yes |
| P2 flagged exercised folds, pooled | 114 / 135 = 0.844 (Wilson 95% 0.774 to 0.896) | >= 0.80 | yes (point estimate) |
| P3 control folds flagged | 3 / 60 = 0.050 (Wilson 95% 0.017 to 0.137) | <= 0.05 | yes, at the bar |
| sanity: C1 flagged | no | never | yes |

Printed by the run (per policy: full benchmark, then folds):

```
R1 late on 5% of processing errors: flagged=True (verifier True, batch False, gate hold []) changed=27 new_hard=21 {'DL-01': 21} mean_cost_diff=11.3; folds exercised 15/20, flagged 15
R2 late on 2% of all cases: flagged=True (verifier True, batch False, gate block ['__pooled__']) changed=202 new_hard=162 {'DL-01': 162, 'INV-01': 4} mean_cost_diff=59.8; folds exercised 20/20, flagged 20
R3 wrong documents cited on visa 13.1: flagged=True (verifier True, batch True, gate block ['buyers_remorse', 'service_failure', '__pooled__']) changed=235 new_hard=1 {'INV-01': 1} mean_cost_diff=187.7; folds exercised 20/20, flagged 6
R4 refund the capture under $25: flagged=True (verifier True, batch False, gate hold []) changed=641 new_hard=641 {'DL-01': 610, 'INV-01': 36, 'SCOPE-01': 212, 'SR-01': 5} mean_cost_diff=8.6; folds exercised 20/20, flagged 20
R5 concede everything under $25: flagged=True (verifier True, batch True, gate hold []) changed=717 new_hard=20 {'INV-01': 20} mean_cost_diff=1.3; folds exercised 20/20, flagged 13
R6 never ask for the cardholder's statement: flagged=True (verifier True, batch False, gate hold []) changed=1997 new_hard=1997 {'PROC-01': 1997} mean_cost_diff=0.0; folds exercised 20/20, flagged 20
R7 report every representment as won: flagged=True (verifier True, batch False, gate hold []) changed=1111 new_hard=1111 {'SR-01': 1111} mean_cost_diff=0.0; folds exercised 20/20, flagged 20
C1 unmodified: flagged=False (verifier False, batch False, gate hold []) changed=0 new_hard=0 {} mean_cost_diff=0.0; folds exercised 0/20, flagged 0
C2 reads and cites in reverse order: flagged=False (verifier False, batch False, gate hold []) changed=9966 new_hard=0 {} mean_cost_diff=0.0; folds exercised 20/20, flagged 0
C3 waits a day when a week is left: flagged=True (verifier True, batch False, gate hold []) changed=9966 new_hard=3 {'INV-01': 3} mean_cost_diff=0.1; folds exercised 20/20, flagged 3
C4 states no win probability: flagged=False (verifier False, batch False, gate hold []) changed=9985 new_hard=0 {} mean_cost_diff=0.0; folds exercised 20/20, flagged 0
```

Which detector fired on exercised folds (verifier / batch / gate): R1 15/0/0,
R2 20/0/0, R3 1/6/0, R4 20/0/0, R5 12/3/0, R6 20/0/0, R7 20/0/0; controls: C3
3/0/0, C2 and C4 nothing. Batch failures: R3 CAL-01 on 6 blocks, R5 CON-01 on 3.

Against the predictions written down before the run:

- R6, R7 (PROC-01, SR-01), R1, R2 (DL-01) and R4 (DL-01, SCOPE-01, INV-01) were caught
  on every exercised fold, as predicted. R1 was exercised in only 15 of 20 folds (27
  changed episodes in 10,000).
- R3 and R5 were the weak ones, as predicted, but not entirely for the predicted
  reason. **R3** costs $188 per case on average over the benchmark (235 changed
  episodes; the lost representments on Visa 13.1 are expensive). The gate blocked it
  on the full benchmark (buyers_remorse, service_failure and pooled) but on no single
  fold. Folds caught it 6 times of 20, through CAL-01: the policy keeps stating its
  usual confidence on representments it now loses. **R5** costs $1.30 per case; the
  gate held even on 10,000 cases (the planning said a per-family regression needs about
  $20 per case at this size). It was flagged on 13 of 20 folds, and the reason
  matters: 12 of those were INV-01 failures, the refund-race over-credit the honest
  reference also trips (docs/experiments/README.md), which conceding more often
  triggers more often. That is detection by a side effect, not by a check that reads
  the regression. For a cost-only regression the gate is the only real detector, and
  it needs far more than 500 cases.
- **The controls were not all clean.** C3 (wait one day when a week is left) was
  flagged on 3 of 20 folds and on the full benchmark, all by INV-01 (ledger
  invariants), on 3 episodes. We did not trace those 3 episodes one by one; the likely
  mechanism is the one behind the honest policy's 19 INV-01 failures: INV-01 charges a
  violation that stands at close to the agent (finding F-8), so a change of timing
  around a settling refund becomes a hard failure. It is what put P3 exactly on its
  bar.

What this does and does not show:

- On this synthetic benchmark, hard checks catch regressions that leave a trace a
  check reads (a missed window, a skipped step, a false claim, an out-of-bounds
  refund) on every fold where they happen.
- Regressions that only cost money are caught by the statistical gate on the full
  10,000 cases when large (R2 at $60, R3 at $188 per case) and missed when small (R5
  at $1.30), as the planning predicted; on 500-case folds the gate caught nothing.
- INV-01's close-time charge for refund races is now the main source of both false
  alarms and incidental detections. Separating "the agent's action caused the
  over-credit" from "a refund the agent did not start settled late" would cut both.
- One synthetic benchmark, one baseline policy and seven hand-written regressions
  chosen before the run; they say nothing about the regressions real agents have.
