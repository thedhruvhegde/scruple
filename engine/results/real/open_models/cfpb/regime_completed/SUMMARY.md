# real models vs baselines, same items

Real CFPB complaints (public domain). Sealed test split, seeded sample.
Models answered blind through the harness described in docs/results/REAL_MODELS.md.
Paired bootstrap over items, 2000 resamples, seed 0.

## regime-2015 (n=22, accuracy)

| system | accuracy | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.636 |  |
| keywords | 0.818 |  |
| tfidf_lr | 0.818 |  |
| claude-haiku | 0.909 | +0.091 [+0.000, +0.227] |
| claude-sonnet | 1.000 | +0.182 [+0.045, +0.364] |
| gpt-oss-120b | 0.909 | +0.091 [-0.091, +0.273] |
| gpt-oss-20b | 0.727 | -0.091 [-0.364, +0.136] |
