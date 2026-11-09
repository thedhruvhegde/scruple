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

(appended after the run)
