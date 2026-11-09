# real models vs baselines, same items

Real CFPB complaints (public domain). Sealed test split, seeded sample.
Models answered blind through the harness described in docs/results/REAL_MODELS.md.
Paired bootstrap over items, 2000 resamples, seed 0.

## outcome-2015 (n=100, auroc)

| system | auroc | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.500 |  |
| keywords | 0.568 |  |
| tfidf_lr | 0.701 |  |
| haiku | 0.550 | -0.150 [-0.318, +0.008] |
| sonnet | 0.606 | -0.095 [-0.213, +0.025] |

## regime-2015 (n=100, accuracy)

| system | accuracy | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.630 |  |
| keywords | 0.800 |  |
| tfidf_lr | 0.870 |  |
| haiku | 0.900 | +0.030 [-0.030, +0.090] |
| sonnet | 0.930 | +0.060 [+0.000, +0.120] |

## routing-2025 (n=50, accuracy)

| system | accuracy | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.480 |  |
| keywords | 0.220 |  |
| tfidf_lr | 0.580 |  |
| haiku | 0.560 | -0.020 [-0.160, +0.120] |
| sonnet | 0.540 | -0.040 [-0.180, +0.100] |
