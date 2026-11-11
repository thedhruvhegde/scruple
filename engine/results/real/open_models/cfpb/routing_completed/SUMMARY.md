# real models vs baselines, same items

Real CFPB complaints (public domain). Sealed test split, seeded sample.
Models answered blind through the harness described in docs/results/REAL_MODELS.md.
Paired bootstrap over items, 2000 resamples, seed 0.

## routing-2025 (n=13, accuracy)

| system | accuracy | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.462 |  |
| keywords | 0.385 |  |
| tfidf_lr | 0.462 |  |
| claude-haiku | 0.615 | +0.154 [+0.000, +0.385] |
| claude-sonnet | 0.462 | +0.000 [-0.231, +0.231] |
| gpt-oss-120b | 0.538 | +0.077 [-0.231, +0.385] |
