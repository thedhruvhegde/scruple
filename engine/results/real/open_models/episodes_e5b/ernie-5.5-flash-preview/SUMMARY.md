# openai-compatible/baidu/ernie-5.5-flash-preview:free/zero-shot on simulated disputes

Seeds 5001-5002 (2 cases, one per seed). Synthetic cases; real model behaviour. Path: api.
References replay the same seeds in-process. Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.

| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward | won/partial | fair outcome (OUT-01) | top hard failures |
|---|---|---|---|---|---|---|---|---|
| openai-compatible/baidu/ernie-5.5-flash-preview:free/zero-shot | 2 | 79.21 | +12.50 [+0.00, +25.00] | 0/2 | 0.000 | 0/2 | 1/2 | SR-01 x2, DL-01 x1 |
| ref:evidence | 2 | 66.71 |  | 2/2 | 0.928 | 0/2 | 1/2 | none |
| ref:always_accept | 2 | 54.21 | -12.50 [-25.00, +0.00] | 2/2 | 0.935 | 0/2 | 1/2 | none |
| ref:oracle | 2 | 66.71 | +0.00 [+0.00, +0.00] | 2/2 | 0.928 | 0/2 | 1/2 | none |

Stops: submitted 2.
Tokens: 99734 in, 7627 out.
