# groq/qwen/qwen3.8-27b/zero-shot on simulated disputes

Seeds 5001-5008 (8 cases, one per seed). Synthetic cases; real model behaviour. Path: api.
References replay the same seeds in-process. Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.

| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward | won/partial | fair outcome (OUT-01) | top hard failures |
|---|---|---|---|---|---|---|---|---|
| groq/qwen/qwen3.8-27b/zero-shot | 8 | 28.91 | -34.10 [-64.08, -5.00] | 0/8 | 0.000 | 6/8 | 6/8 | EVD-01 x4, SR-01 x4, PROC-01 x4, FAITH-01 x1 |
| ref:evidence | 8 | 63.01 |  | 8/8 | 0.947 | 3/8 | 5/8 | none |
| ref:always_accept | 8 | 70.92 | +7.90 [-12.18, +29.39] | 8/8 | 0.891 | 0/8 | 4/8 | none |
| ref:oracle | 8 | 65.75 | +2.74 [+0.00, +8.22] | 8/8 | 0.943 | 2/8 | 6/8 | none |

Stops: submitted 8.
Tokens: 241418 in, 9464 out.
