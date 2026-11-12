# groq/qwen/qwen3.8-27b/zero-shot on simulated disputes

Seeds 5001-5011 (11 cases, one per seed). Synthetic cases; real model behaviour. Path: api.
References replay the same seeds in-process. Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.

| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward | won/partial | fair outcome (OUT-01) | top hard failures |
|---|---|---|---|---|---|---|---|---|
| groq/qwen/qwen3.8-27b/zero-shot | 11 | 35.50 | -24.80 [-48.99, -2.41] | 1/11 | 0.090 | 8/11 | 9/11 | EVD-01 x5, SR-01 x5, PROC-01 x5, FAITH-01 x1 |
| ref:evidence | 11 | 60.30 |  | 11/11 | 0.961 | 5/11 | 8/11 | none |
| ref:always_accept | 11 | 123.74 | +63.44 [-1.07, +152.34] | 11/11 | 0.875 | 0/11 | 5/11 | none |
| ref:oracle | 11 | 62.29 | +1.99 [+0.00, +5.98] | 11/11 | 0.956 | 4/11 | 9/11 | none |

Stops: submitted 11.
Tokens: 313964 in, 12918 out.
