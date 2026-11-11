# real models vs baselines, same items

Real CFPB complaints (public domain). Sealed test split, seeded sample.
Models answered blind through the harness described in docs/results/REAL_MODELS.md.
Paired bootstrap over items, 2000 resamples, seed 0.

## outcome-2015 (n=75, auroc)

| system | auroc | vs tfidf_lr (95% ci) |
|---|---|---|
| majority | 0.500 |  |
| keywords | 0.560 |  |
| tfidf_lr | 0.727 |  |
| claude-haiku | 0.606 | -0.121 [-0.301, +0.054] |
| claude-sonnet | 0.605 | -0.122 [-0.260, +0.012] |
| gpt-oss-120b | 0.452 | -0.275 [-0.464, -0.085] |
| gpt-oss-20b | 0.422 | -0.305 [-0.482, -0.116] |
