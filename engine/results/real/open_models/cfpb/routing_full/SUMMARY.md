# real models vs baselines, same items

Real CFPB complaints (public domain). Sealed test split, seeded sample.
Models answered blind through the harness described in docs/results/REAL_MODELS.md.
Paired bootstrap over items, 2000 resamples, seed 0.

## routing-2025 (n=50, accuracy)

| system | accuracy | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.480 |  |
| keywords | 0.220 |  |
| tfidf_lr | 0.580 |  |
| claude-haiku | 0.560 | -0.020 [-0.160, +0.120] |
| claude-sonnet | 0.540 | -0.040 [-0.180, +0.100] |
| gpt-oss-120b | 0.540 | -0.040 [-0.220, +0.140] |
