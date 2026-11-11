# real models vs baselines, same items

Real CFPB complaints (public domain). Sealed test split, seeded sample.
Models answered blind through the harness described in docs/results/REAL_MODELS.md.
Paired bootstrap over items, 2000 resamples, seed 0.

## regime-2015 (n=100, accuracy)

| system | accuracy | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.630 |  |
| keywords | 0.800 |  |
| tfidf_lr | 0.870 |  |
| claude-haiku | 0.900 | +0.030 [-0.030, +0.090] |
| claude-sonnet | 0.930 | +0.060 [+0.000, +0.120] |
| gpt-oss-120b | 0.910 | +0.040 [-0.010, +0.100] |
| gpt-oss-20b | 0.840 | -0.030 [-0.100, +0.040] |
