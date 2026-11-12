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

## Open models through real APIs, same items (E5, added 2026-11-11)

Pre-registered in docs/experiments/E5_open_models.md. Real CFPB data, as above.

- The open models answered the same prompt files through the raw Groq API: the
  same `system` and `prompt` text, temperature 0, no tools, 2,048 max tokens.
- Grading used the existing grader:
  `uv run python scripts/compare_real_models.py results/real/open_models/cfpb/regime_completed`.

**Coverage.** The account's daily cap of 200,000 tokens per model stopped the
runs. Not-run items are counted, not graded.

- regime-2015: gpt-oss-120b answered 26 of 100 items, gpt-oss-20b 22.
- outcome-2015 and routing-2025: no open model ran.
- qwen/qwen3.8-27b: no CFPB items.

The table covers the 22 regime items both gpt-oss models answered, the first 22
in prompt order. Baselines and Claude are scored on the same 22.

| system | harness | regime-2015 accuracy (n=22) | vs tf-idf + lr (95% ci) | valid replies |
|---|---|---|---|---|
| majority | baseline | 0.636 | | |
| keywords | baseline | 0.818 | | |
| tf-idf + lr | baseline | 0.818 | | |
| claude haiku | Claude Code subagent | 0.909 | +0.091 [+0.000, +0.227] | 22/22 |
| claude sonnet | Claude Code subagent | 1.000 | +0.182 [+0.045, +0.364] | 22/22 |
| openai/gpt-oss-120b | Groq API | 0.909 | +0.091 [-0.091, +0.273] | 22/22 |
| openai/gpt-oss-20b | Groq API | 0.727 | -0.091 [-0.364, +0.136] | 20/22 |

- **The harnesses differ.** Claude ran as subagents reading a prompt file; the
  open models got a single API completion per item. The text sent was the same.
- **gpt-oss-20b's two invalid replies were empty.** It spent the 2,048-token
  allowance reasoning. They are graded as wrong.
- **22 items.** No difference from TF-IDF is distinguishable for either open
  model. The outcome-judgement question (AUROC) is untested for open models.

### E5 final (added 2026-11-11, session 2)

This replaces the 22-item E5 table above. Same item ids, same grader, same
`system` and `prompt` text through the raw Groq API (temperature 0, no tools, 2,048
max tokens). Real CFPB data. Each table covers the first k items, in prompt order,
that every graded system answered.

Reproduce: `uv run python scripts/compare_real_models.py
results/real/open_models/cfpb/<dir>`, with dir one of `regime_full`, `outcome_full`,
`outcome_completed` or `routing_completed`.

| task | n | majority | keywords | tf-idf + lr | claude haiku | claude sonnet | gpt-oss-120b | gpt-oss-20b |
|---|---|---|---|---|---|---|---|---|
| regime-2015 (accuracy) | 100 | 0.630 | 0.800 | 0.870 | 0.900 | 0.930 | 0.910 | 0.840 |
| outcome-2015 (auroc) | 100 | 0.500 | 0.568 | 0.701 | 0.550 | 0.606 | 0.425 | not run |
| outcome-2015 (auroc) | 75 | 0.500 | 0.560 | 0.727 | 0.606 | 0.605 | 0.452 | 0.422 |
| routing-2025 (issue acc.) | 13 | 0.462 | 0.385 | 0.462 | 0.615 | 0.462 | 0.538 | not run |

Against TF-IDF + LR (95% CI):

- gpt-oss-120b:
  - regime: +0.040 [-0.010, +0.100]
  - outcome (n=100): -0.275 [-0.450, -0.102]
  - routing (n=13): +0.077 [-0.231, +0.385]
- gpt-oss-20b:
  - regime: -0.030 [-0.100, +0.040]
  - outcome (n=75): -0.305 [-0.482, -0.116]

What it shows:

- **Coverage.** The Groq per-day token cap (200,000 per model) stopped the runs.
  - Not run: gpt-oss-20b, 25 outcome and 50 routing items; gpt-oss-120b, 37
    routing items; qwen/qwen3.8-27b, every CFPB item.
  - Not-run items are counted, never graded.
- **The same split as Claude, stronger.**
  - Rule knowledge is good: both gpt-oss models beat the keyword rules on regime
    and are level with TF-IDF.
  - Outcome judgement is worse than the classifier, and below chance: AUROC
    0.43-0.45 with every reply parsed.
- **gpt-oss-20b had 3 empty regime replies.** It spent the 2,048-token allowance
  reasoning. They are graded as wrong.
- **The harnesses differ.** Claude ran as subagents reading a prompt file; the open
  models got one API completion per item. The text was the same.

### E5 continuation (added 2026-11-12, session 3)

This replaces the session-2 E5 table above. Same item ids, grader and prompt text.
Real CFPB data. Reproduce with `uv run python scripts/compare_real_models.py
results/real/open_models/cfpb/<dir>`, dir one of `regime_full`, `outcome_full`,
`routing_full` or `routing_completed_s3`.

| task | n | majority | keywords | tf-idf + lr | claude haiku | claude sonnet | gpt-oss-120b | gpt-oss-20b |
|---|---|---|---|---|---|---|---|---|
| regime-2015 (accuracy) | 100 | 0.630 | 0.800 | 0.870 | 0.900 | 0.930 | 0.910 | 0.840 |
| outcome-2015 (auroc) | 100 | 0.500 | 0.568 | 0.701 | 0.550 | 0.606 | 0.425 | 0.399 |
| routing-2025 (issue acc.) | 50 | 0.480 | 0.220 | 0.580 | 0.560 | 0.540 | 0.540 | not complete |
| routing-2025 (issue acc.) | 41 | 0.488 | 0.220 | 0.561 | 0.585 | 0.561 | 0.537 | 0.561 |

Against TF-IDF + LR (95% CI):

- gpt-oss-120b: regime +0.040 [-0.010, +0.100]; outcome -0.275 [-0.450, -0.102];
  routing (n=50) -0.040 [-0.220, +0.140].
- gpt-oss-20b: regime -0.030 [-0.100, +0.040]; outcome (n=100) -0.302 [-0.471,
  -0.131]; routing (n=41) +0.000 [-0.195, +0.195].

What changed:

- **Outcome is now complete for both gpt-oss models.** Both rank complaints below
  chance (AUROC 0.425 and 0.399) with every reply parsed, and both are below
  TF-IDF with CIs wholly below 0.
- **Routing:** gpt-oss-120b answered all 50; gpt-oss-20b 41 of 50 before the Groq
  daily cap stopped the session. Both are level with TF-IDF and Claude.
  gpt-oss-20b had 3 invalid routing replies (one empty after 2,048 reasoning
  tokens, two with labels outside the list), graded as wrong.
- **Not run:** gpt-oss-20b routing items 42-50 and every qwen/qwen3.8-27b item.
  Counted, never graded.
