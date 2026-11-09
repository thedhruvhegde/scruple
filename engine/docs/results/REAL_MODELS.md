# Real models on real complaints

Two Claude models answered held-out, real CFPB complaints blind. A grader scored
each reply against the real label in the data. Nothing here is simulated.

Tables: `results/real/models/SUMMARY.md` (numbers) and `summary.json` (machine readable).
Reproduce the comparison: `uv run python scripts/compare_real_models.py results/real/models`.

## Setup

- Data: CFPB Consumer Complaint Database narratives, public domain (see docs/data/CFPB.md).
  Frozen splits `data/real/v1`; items drawn from the sealed test split with
  `scruple realdata prompt --split test --seed 7` (n=100, or n=50 for routing).
- Tasks (graded by code against real fields, never by a model):
  - `outcome-2015`: P(complaint closed with relief). Metric: AUROC.
  - `regime-2015`: Reg E (12 CFR 1005.11) or Reg Z (12 CFR 1026.13), with a section citation. Metric: accuracy.
  - `routing-2025`: the issue the consumer filed under. Metric: issue accuracy.
- Models: Claude Haiku and Claude Sonnet (current releases, November 2026), run as
  Claude Code subagents. Each got the same `system` and `prompt` text the API runner
  sends. Each could read only its prompt file. It had no web access and no repo access.
  It was told not to use code to decide answers. Every reply was graded as written. A
  malformed reply counts as wrong, or as p=0.5 for AUROC.
- Baselines: majority, keyword rules, and TF-IDF + logistic regression. The baselines
  were fitted on train, chosen on dev, then scored on exactly the same items as the models.
- Comparison: a paired bootstrap over items (2,000 resamples) of model minus TF-IDF on
  the primary metric.

## Results (same items)

| task | n | majority | keywords | tf-idf + lr | haiku | sonnet |
|---|---|---|---|---|---|---|
| outcome-2015 (auroc) | 100 | 0.500 | 0.568 | 0.701 | 0.550 | 0.606 |
| regime-2015 (accuracy) | 100 | 0.630 | 0.800 | 0.870 | 0.900 | 0.930 |
| routing-2025 (issue acc.) | 50 | 0.480 | 0.220 | 0.580 | 0.560 | 0.540 |

Paired differences against TF-IDF, with 95% CI:

| task | haiku | sonnet |
|---|---|---|
| outcome-2015 | -0.150 [-0.318, +0.008] | -0.095 [-0.213, +0.025] |
| regime-2015 | +0.030 [-0.030, +0.090] | +0.060 [+0.000, +0.120] |
| routing-2025 | -0.020 [-0.160, +0.120] | -0.040 [-0.180, +0.100] |

Every reply was valid. Full-split baseline numbers are in REAL_BASELINES.md.

## What it shows

- **Rule knowledge is strong.** Both models placed complaints under the right
  error-resolution regime and cited a real section 90–93% of the time. They beat
  keyword rules, and Sonnet edges past the trained classifier (CI touches 0).
- **Outcome judgement is weak.** Neither model beat a simple classifier trained on
  real outcomes at predicting whether a complaint ends in relief. Haiku is near
  chance. The models' stated probabilities clump on a few values (0.2, 0.3), which
  is coarse calibration.
- **For scruple, the point is this.** An agent can know the rules and still misjudge
  which cases are worth contesting. That error costs money. Self-report cannot show
  it, and a verifier anchored to real outcomes can.

## Limits

- **Small n.** CIs are wide, and only one regime difference excludes zero, at the edge.
- **Subagent harness.** The models ran as Claude Code subagents, not through the raw
  API, so system scaffolding differs from `scruple realdata run`. Isolation came from
  instructions and file layout; tool use was not technically sandboxed. Transcripts
  show 7–12 tool calls per run: reading the prompts and writing the answers.
- **What CFPB data is.** These are complaints to a regulator, a self-selected
  sample. The outcome is the company's response, not a card-network decision.
- **Regime labels.** The label comes from the product field by a documented mapping
  (docs/data/CFPB.md). It is not a legal finding.
- **Coverage.** No other vendors' models were run; no API key was available in this
  environment. `scruple realdata run` covers that once a key is set.
