# real-data baselines (CFPB complaints)

Real data, not synthetic. Dataset: CFPB Consumer Complaint Database narratives,
public domain (17 USC 105), frozen splits `data/real/v1` (data card:
docs/data/CFPB.md). Every number below is copied from `results/real/<task>.json`,
produced by:

```bash
uv run scruple realdata baselines        # about 10 minutes, deterministic
```

Each baseline is fitted on train, its one hyperparameter (LR `C`) is chosen on dev,
and it is scored once on the sealed test split. Answers go through the same strict
JSON parser and grader a model faces; no baseline answer was malformed. Brackets are
95% percentile-bootstrap intervals (1,000 resamples of test instances, seed 0).
Baselines are described in `scruple/realdata/baselines.py`; scikit-learn 1.9.1.

## Issue routing

`routing-2015`: narrative -> issue, 47 labels, test n = 3,024.

| baseline | accuracy | macro-F1 |
|---|---|---|
| majority ("Account opening, closing, or management") | 0.146 [0.135, 0.159] | 0.005 [0.005, 0.006] |
| keywords (label-word overlap) | 0.121 [0.110, 0.134] | 0.074 [0.056, 0.090] |
| tfidf_lr (C = 8) | 0.435 [0.418, 0.452] | 0.217 [0.202, 0.235] |

`routing-2025`: narrative -> issue and sub-issue, test n = 1,598.

| baseline | issue accuracy | issue macro-F1 | pair accuracy | pair macro-F1 |
|---|---|---|---|---|
| majority ("Managing an account / Deposits and withdrawals") | 0.553 [0.529, 0.577] | 0.079 [0.077, 0.105] | 0.193 [0.175, 0.213] | 0.010 [0.010, 0.013] |
| keywords | 0.214 [0.195, 0.232] | 0.155 [0.145, 0.214] | 0.108 [0.093, 0.123] | 0.069 [0.063, 0.090] |
| tfidf_lr (C = 8) | 0.631 [0.606, 0.655] | 0.313 [0.298, 0.425] | 0.391 [0.367, 0.417] | 0.165 [0.160, 0.205] |

tfidf_lr per-class recall on the largest routing-2015 classes: account opening,
closing or management 0.790 (n 443), deposits and withdrawals 0.502 (279), billing
disputes 0.619 (244), funds low 0.710 (183), "Other" 0.339 (165), identity theft /
fraud 0.503 (143), using a debit or ATM card 0.447 (123). 18 of the 47 classes have
recall 0: seventeen with 13 or fewer test rows, plus "Credit card protection / Debt
protection" (53 rows).

## Outcome prediction (relief vs explanation only)

| task | baseline | AUROC | Brier | ECE (10 bins) | balanced acc. at 0.5 |
|---|---|---|---|---|---|
| outcome-2015 (n 2,975, relief 0.344) | majority | 0.500 [0.500, 0.500] | 0.226 [0.221, 0.230] | 0.016 [0.001, 0.033] | 0.500 |
| | keywords | 0.579 [0.560, 0.597] | 0.221 [0.216, 0.226] | 0.020 [0.006, 0.037] | 0.500 |
| | tfidf_lr (C = 2) | 0.722 [0.703, 0.741] | 0.191 [0.184, 0.198] | 0.018 [0.015, 0.038] | 0.629 [0.612, 0.645] |
| outcome-2025 (n 1,591, relief 0.185) | majority | 0.500 [0.500, 0.500] | 0.151 [0.139, 0.163] | 0.007 [0.000, 0.026] | 0.500 |
| | keywords | 0.517 [0.490, 0.546] | 0.151 [0.139, 0.163] | 0.008 [0.002, 0.029] | 0.500 |
| | tfidf_lr (C = 2) | 0.704 [0.671, 0.737] | 0.140 [0.129, 0.152] | 0.023 [0.015, 0.044] | 0.503 [0.500, 0.509] |

## Regulation tagging (Reg E 1005.11 vs Reg Z 1026.13)

`regime-2015`: test n = 764 (437 reg_z, 327 reg_e). Every baseline cites a fixed,
real paragraph per regime, so citation existence is 1.000 for all of them and
strict accuracy equals regime accuracy.

| baseline | accuracy | macro-F1 | balanced acc. | recall reg_e | recall reg_z | strict |
|---|---|---|---|---|---|---|
| majority (reg_z) | 0.572 [0.537, 0.606] | 0.364 [0.349, 0.377] | 0.500 | 0.000 | 1.000 | 0.572 |
| keywords | 0.783 [0.754, 0.810] | 0.758 [0.726, 0.789] | 0.752 [0.725, 0.780] | 0.541 | 0.963 | 0.783 |
| tfidf_lr (C = 2) | 0.882 [0.859, 0.907] | 0.877 [0.852, 0.904] | 0.872 [0.846, 0.898] | 0.798 | 0.945 | 0.882 |

tfidf_lr confusion (gold -> predicted): reg_e 261 reg_e / 66 reg_z; reg_z 24 reg_e /
413 reg_z.

## What this means

- **Routing is hard, and the labels are noisy.** A bag of words gets 0.435 on 47
  issue labels and 0.217 macro-F1. Many issues overlap ("Other", "Customer service",
  "Billing statement" vs "Billing disputes"), and the label is the consumer's pick
  on a form. The number to beat for a model is the tfidf_lr row, not 1.0; nobody
  knows the ceiling, because no human agreement study exists for these labels.
- **Outcome is weakly predictable from text.** AUROC about 0.70-0.72 with TF-IDF
  plus product, issue and company tokens. Probabilities are reasonably calibrated
  (ECE about 0.02), but at an 18% base rate (2025) almost no prediction crosses
  0.5, so balanced accuracy at 0.5 is uninformative there; read AUROC and Brier.
  Relief is the company's own report and depends on the company, so a model that
  scores well may be learning issuer behaviour, not the merits of the complaint.
- **Regime tagging is mostly product detection.** Keywords alone reach 0.783; the
  label is derived from the product field, and the narrative usually says "credit
  card" or "debit card". Its errors are mostly reg_e cases called reg_z. This task
  checks that a model can tell the two regimes apart and cite a real paragraph; it
  does not check legal reasoning about the facts.
- **The keyword routing rule is worse than majority on accuracy** (0.121 vs 0.146)
  but better on macro-F1: matching label words spreads predictions over rare labels.

## Caveats

- Percentile-bootstrap intervals for macro-F1 with many rare classes are skewed
  (the routing-2025 issue interval [0.298, 0.425] sits mostly above the point
  0.313): classes missing from a resample drop out of the average. Treat them as
  rough. ECE is biased upward under resampling, so its interval can sit above the
  point estimate (outcome-2015 tfidf_lr). Majority and keyword intervals of width
  zero are exact: those predictions do not vary.
- `C = 8` is the top of the grid for both routing tasks, so tfidf_lr routing may be
  slightly under-tuned. The grid was fixed before looking at test.
- One run per configuration; the fits are deterministic (lbfgs), and rerunning
  `regime-2015` reproduced the result file byte for byte.
- Training used the committed 45% (2015) and 40% (2025) train splits, not every
  available row (see the data card), so these baselines are a little weaker than a
  full-data fit would be.
- No LLM has been run on these tasks yet. `scruple realdata prompt|run|grade` is the
  harness for that (docs/data/CFPB.md, `scruple/realdata/cli.py`).
