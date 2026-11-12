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
| claude-haiku | 0.550 | -0.150 [-0.318, +0.008] |
| claude-sonnet | 0.606 | -0.095 [-0.213, +0.025] |
| gpt-oss-120b | 0.425 | -0.275 [-0.450, -0.102] |
| gpt-oss-20b | 0.399 | -0.302 [-0.471, -0.131] |
