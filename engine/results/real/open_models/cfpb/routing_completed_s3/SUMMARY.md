# real models vs baselines, same items

Real CFPB complaints (public domain). Sealed test split, seeded sample.
Models answered blind through the harness described in docs/results/REAL_MODELS.md.
Paired bootstrap over items, 2000 resamples, seed 0.

## routing-2025 (n=41, accuracy)

| system | accuracy | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.488 |  |
| keywords | 0.220 |  |
| tfidf_lr | 0.561 |  |
| claude-haiku | 0.585 | +0.024 [-0.122, +0.171] |
| claude-sonnet | 0.561 | +0.000 [-0.146, +0.146] |
| gpt-oss-120b | 0.537 | -0.024 [-0.220, +0.171] |
| gpt-oss-20b | 0.561 | +0.000 [-0.195, +0.195] |
