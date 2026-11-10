# Pre-registered success criteria (template)

Fill this in during week 1, sign it, and commit it (or store a hash of it with both
sides) **before any partner data is loaded**. After that, the hypotheses, metrics, pass
bars and sample sizes do not change. If something has to change, add a dated
amendment below the signatures that says what changed and why, and report both the
original and the amended criterion in the readout.

The result of every criterion is reported in the readout, whether it passes, fails or
did not reach its sample size. This follows the rule in docs/ROADMAP.md for
pre-registered experiments: write the hypothesis and pass bar down before running,
publish the result either way.

## Header

| field | value |
|---|---|
| partner | `[PARTNER]` |
| workflow | card disputes |
| current policy | `[rule set / agent version id]` |
| candidate | `[rule id and version, or agent version]`, content hash `[hash]` |
| history window | `[start date]` to `[end date]`, resolved cases only |
| shadow window | `[start date]` to `[end date]` |
| signed by (partner) | `[name, role, date]` |
| signed by (scruple) | `[name, role, date]` |
| hash of this file at signing | `[sha256]` |

## Hypotheses

Each row is one criterion. H1 to H4 can be measured without knowing what would have
happened under an action that was not taken. H5 cannot, and is optional.

| id | hypothesis | metric (how it is computed) | pass bar | sample size | source in the readout |
|---|---|---|---|---|---|
| H1 | The gate catches regressions planted in the partner's own history | share of planted defective candidates that the gate blocks; `[K]` plants agreed in week 1, each with the reason code it should hurt | all `[K]` blocked, each flagged in the right reason code | `[K]` plants; each needs enough cases in its reason code to show the defect (count from the backtest) | backtest report, scorecards |
| H2 | The candidate adds no hard policy violations | new violations of the partner's hard constraints on history and in shadow (exact counts, observable fields only) | `[0]` new violations | all cases in both windows | backtest report, shadow summary |
| H3 | The self-report gap can be measured from the ledger | the partner's reported metric (for example win rate on its dashboard) against the same metric recomputed from balance movements, per reason code | gap reported with a 95% interval for every reason code with at least `[n]` resolved cases | `[n]` per reason code | backtest report |
| H4 | The gate is reproducible in the partner's CI | rerunning the same candidate and config gives a byte-identical scorecard and the same audit head | identical on `[3]` reruns | n/a | gate scorecards |
| H5 (optional) | The candidate lowers cost per case by at least `[MDI]` dollars in every reason code | paired cost difference, current minus candidate, per reason code; cost on changed decisions from the outcome model stated in week 1, labelled model-based | anytime-valid test rejects "no improvement" at family-wise `alpha = [0.05]` in every reason code | from `scripts/pilot_plan.py`, see below | backtest report |

Notes on the metrics:

- "Blocked" means the gate verdict is BLOCK (or HOLD, if `fail_on_hold = true`, which is
  the default; docs/CI.md).
- H3 compares two numbers; it does not pass because the gap is large or small. It passes
  when the gap is measured with the stated precision.
- H5's test is the stratified betting test in `scruple/stats` (docs/math/01 to 03). The
  sample-size planner assumes a paired design; a two-sample canary design is not
  covered (docs/math/README.md, "What this does not cover").

## What counts as failure

A criterion **fails** if:

- H1: any planted regression is promoted, or is blocked only for a reason unrelated to
  its defect;
- H2: the candidate adds any hard violation above the pass bar;
- H3: the ledger and the partner's own numbers cannot be reconciled for a reason code
  that has enough cases (for example balance movements missing for a share of cases);
- H4: any rerun differs;
- H5: the test does not reject at the planned sample size. If the sample size was not
  reached, the result is **not enough data**, which is reported as such and is not a
  pass.

The pilot as a whole is reported as: criteria passed, criteria failed, criteria without
enough data. There is no single blended score.

## Sample size (H5): `scripts/pilot_plan.py`

The planner wraps `scruple.stats.planning` (docs/math/03_sample_size.md). Inputs:

| input | flag | who provides it |
|---|---|---|
| monthly dispute volume | `--monthly-volume` | partner |
| reason-code mix (shares summing to 1) | `--mix fraud=0.5,...` | partner, from history |
| minimum detectable improvement, USD per case | `--min-improvement` | partner and scruple agree |
| cap on the per-case cost difference, USD | `--cost-cap` | partner: largest dispute amount plus fees that one decision can swing |
| per-case standard deviation of the difference, USD | `--sd` (omit for worst case) | estimated from history, then fixed here |
| share of traffic shadowed | `--shadow-fraction` (default 1) | partner |
| weeks until a shadowed case resolves | `--outcome-lag-weeks` | partner, from its own history |

Outputs: resolved cases needed, months of history that contain them, and weeks of
shadow traffic (plus lag) to see them live. The plan guarantees power of at least
`(1 - beta)(1 - gamma)` per reason code at the stated improvement, with family-wise
error at most `alpha`, for every distribution with that cap and standard deviation.
Defaults: `alpha = 0.05`, `beta = 0.2`, `gamma = 0.05`, Bernstein bound, optimised
allocation of `alpha` across reason codes.

### Worked example (illustrative inputs, not a partner's)

All inputs below are ASSUMPTIONS chosen to match the illustration in
docs/math/03_sample_size.md (cap $100, improvement $5, sd $20); the reason-code mix and
volume are made up for the example. Output pasted from a real run:

```
$ uv run python scripts/pilot_plan.py --monthly-volume 4000 \
    --mix fraud=0.5,not_received=0.3,not_as_described=0.2 \
    --min-improvement 5 --cost-cap 100 --sd 20
pilot plan (inputs are the partner's or illustrative; no outcome is predicted)
  monthly volume 4,000, detect $5 per case, cap $100, sd $20
  alpha 0.05 (family-wise), power 0.8 per reason code, count risk gamma 0.05, bernstein, optimised allocation

  reason                share  alpha_k   cases    total  floor
  fraud                 0.500  5.9e-06   1,477    3,044    188
  not_as_described      0.200    0.047     573    3,044     41
  not_received          0.300    0.003     872    3,044     86

  total resolved cases needed: 3,044 (binding: fraud)
  months of resolved history:  0.8
  shadow weeks of traffic:     3.3 (shadowing 100% of traffic)
  shadow weeks incl. lag:      3.3 (outcome lag 0 weeks)
  fits a 3-week shadow window: no

  cases = cases of that reason code; total = all cases so that reason code
  reaches its count with probability 1 - gamma; floor = fewest cases any valid
  nonparametric test could use at that level (docs/math/03, Proposition 3.3).
```

Reading: with these inputs, one month of resolved history is enough for the backtest,
but the shadow run needs 3.3 weeks of traffic before any outcome lag, so it does not
fit in week 4 alone. With a real outcome lag it fits even less.

### The same question without a standard deviation bound

If the partner cannot bound the per-case standard deviation, the planner must assume
the worst case allowed by the cap. Same tool, different illustrative inputs (lower
volume, $10 improvement, $250 cap, 8-week outcome lag), pasted from a real run:

```
$ uv run python scripts/pilot_plan.py --monthly-volume 1000 \
    --mix fraud=0.6,not_received=0.3,not_as_described=0.1 \
    --min-improvement 10 --cost-cap 250 --outcome-lag-weeks 8
pilot plan (inputs are the partner's or illustrative; no outcome is predicted)
  monthly volume 1,000, detect $10 per case, cap $250, sd worst case
  alpha 0.05 (family-wise), power 0.8 per reason code, count risk gamma 0.05, bernstein, optimised allocation

  reason                share  alpha_k   cases    total  floor
  fraud                 0.600  6.6e-17  70,570  118,078    748
  not_as_described      0.100     0.05  11,639  118,079     49
  not_received          0.300  1.9e-07  35,165  118,078    304

  total resolved cases needed: 118,079 (binding: not_as_described)
  months of resolved history:  118.1
  shadow weeks of traffic:     511.7 (shadowing 100% of traffic)
  shadow weeks incl. lag:      519.7 (outcome lag 8 weeks)
  fits a 3-week shadow window: no
  ...
```

Reading: without a variance bound, detecting $10 per case under a $250 cap needs
about ten years of this volume. This is the "cap tax" of docs/math/03 Proposition 3.3:
every valid nonparametric test pays for a loose cap. The practical consequences for a
pilot:

- estimate the standard deviation from history in week 3, add a margin, and fix it in
  an amendment before the shadow window starts (never after seeing shadow results);
- or narrow H5 to the reason codes where the candidate changes decisions;
- or drop H5 and keep H1 to H4, which need no sample-size miracle.

## Signatures

| | name | role | date |
|---|---|---|---|
| partner | | | |
| scruple | | | |

## Amendments

| date | criterion | change | reason | agreed by |
|---|---|---|---|---|
| | | | | |


## Sample size with variance reduction (appended; synthetic evidence, illustrative inputs)

docs/math/07 adds variance reduction to the statistics. What it changes for H5,
with the same illustrative inputs as the worked example above (not a partner's).
Pasted from real runs of `scripts/exp_e3_pilot.py` (same planner, Bernstein,
optimised alpha):

```
$ uv run python scripts/exp_e3_pilot.py --monthly-volume 4000 \
    --mix fraud=0.5,not_received=0.3,not_as_described=0.2 \
    --min-improvement 5 --cost-cap 100 --sd 20 --variance-ratio 0.0049
pilot plan before and after variance reduction (illustrative inputs; no outcome
is predicted). H5: improvement in every reason code, bernstein, optimised alpha.
  monthly volume 4,000, detect $5/case, cap $100, paired sd $20, outcome lag 0 weeks

  design                                                   cases   months    weeks     +lag
  paired (scripts/pilot_plan.py, today)                    3,044      0.8      3.3      3.3  (fraud)
  no common random numbers (sd $286, ratio 0.0049)       303,469     75.9    328.8    328.8  (not_as_described)
  paired + control variate, R^2 0.25, theta_max 0.5        3,677        1        4        4  (fraud)
  paired + control variate, R^2 0.5, theta_max 0.5         3,172      0.8      3.5      3.5  (fraud)
  paired + control variate, R^2 0.75, theta_max 0.5        2,595      0.7      2.9      2.9  (fraud)

  cases = resolved cases in total; months = months of resolved history at this
  volume; weeks = weeks of shadow traffic (+lag: plus the outcome lag).
  no-CRN row: cap raised to $286 so the sd fits under it.

$ ... same, with --theta-max 0.2 (a tighter bound on the control-variate coefficient)
  paired + control variate, R^2 0.25, theta_max 0.2        3,059      0.8      3.4      3.4  (not_as_described)
  paired + control variate, R^2 0.5, theta_max 0.2         2,590      0.7      2.9      2.9  (not_as_described)
  paired + control variate, R^2 0.75, theta_max 0.2        2,067      0.6      2.3      2.3  (not_as_described)
```

Reading:

- **Common random numbers are already in the "today" row, and they are most of the
  saving.** The paired plan assumes the counterfactual cost is computed for the same
  case under the same outcome draws. If the backtest's outcome model instead drew the
  counterfactual outcome independently (variance ratio 0.0049, measured on synthetic
  episodes in experiment E3 for R5), the same question would need about 300,000
  resolved cases: 76 months of history instead of 0.8. The outcome model stated in
  week 1 must reuse the realised draws wherever the decision did not change.
- **Control variates help only with a known covariate mean and a tight coefficient
  bound.** With `R^2 = 0.75` and `theta_max = 0.2` the plan falls from 3,044 to 2,067
  cases (0.8 to 0.6 months, 3.3 to 2.3 shadow weeks, which then fits the 3-week
  window); with `theta_max = 0.5` the wider bet range eats most of the gain, and at
  `R^2 = 0.25` the plan does not improve. The bound cannot be set below the
  coefficient the covariate needs: in cap units that is at least
  `sqrt(R^2) x sd / cap` (0.17 here for `R^2 = 0.75`), so 0.2 is about the tightest
  honest choice for these inputs. `R^2` and the covariate mean must come from
  history before the shadow window and be written into an amendment; they are
  ASSUMPTIONS here.
- Stratified betting (the other tool in docs/math/07) does not shorten H5: H5 asks
  for an improvement in *every* reason code, which needs one certificate per code. It
  shortens the opposite question ("did any reason code get worse?"), used for
  blocking.
