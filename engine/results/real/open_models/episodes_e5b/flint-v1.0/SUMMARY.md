# openai-compatible/recursive/flint-v1.0:free/zero-shot on simulated disputes

Seeds 5001-5002 (2 cases, one per seed). Synthetic cases; real model behaviour. Path: api.
References replay the same seeds in-process. Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.

| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward | won/partial | fair outcome (OUT-01) | top hard failures |
|---|---|---|---|---|---|---|---|---|
| openai-compatible/recursive/flint-v1.0:free/zero-shot | 2 | 46.73 | -19.98 [-39.96, +0.00] | 0/2 | 0.000 | 1/2 | 0/2 | EVD-01 x1, PROC-01 x1, DL-01 x1 |
| ref:evidence | 2 | 66.71 |  | 2/2 | 0.928 | 0/2 | 1/2 | none |
| ref:always_accept | 2 | 54.21 | -12.50 [-25.00, +0.00] | 2/2 | 0.935 | 0/2 | 1/2 | none |
| ref:oracle | 2 | 66.71 | +0.00 [+0.00, +0.00] | 2/2 | 0.928 | 0/2 | 1/2 | none |

Stops: submitted 2.
Tokens: 42770 in, 3015 out.
