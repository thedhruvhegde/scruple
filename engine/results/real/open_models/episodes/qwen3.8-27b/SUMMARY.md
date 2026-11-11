# groq/qwen/qwen3.8-27b/zero-shot on simulated disputes

Seeds 5001-5004 (4 cases, one per seed). Synthetic cases; real model behaviour. Path: api.
References replay the same seeds in-process. Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.

| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward | won/partial | fair outcome (OUT-01) | top hard failures |
|---|---|---|---|---|---|---|---|---|
| groq/qwen/qwen3.8-27b/zero-shot | 4 | 37.80 | -52.73 [-86.23, -19.23] | 1/4 | 0.247 | 3/4 | 3/4 | EVD-01 x2, SR-01 x1, PROC-01 x1 |
| ref:evidence | 4 | 90.53 |  | 4/4 | 0.927 | 0/4 | 2/4 | none |
| ref:always_accept | 4 | 78.03 | -12.50 [-25.00, +0.00] | 4/4 | 0.935 | 0/4 | 2/4 | none |
| ref:oracle | 4 | 90.53 | +0.00 [+0.00, +0.00] | 4/4 | 0.924 | 0/4 | 2/4 | none |

Stops: http_error 1, submitted 3.
Tokens: 122823 in, 4401 out.
