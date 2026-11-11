# openai-compatible/google/gemma-4-26b-a4b-it:free/zero-shot on simulated disputes

Seeds 5001 (1 cases, one per seed). Synthetic cases; real model behaviour. Path: api.
References replay the same seeds in-process. Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.

| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward | won/partial | fair outcome (OUT-01) | top hard failures |
|---|---|---|---|---|---|---|---|---|
| openai-compatible/google/gemma-4-26b-a4b-it:free/zero-shot | 1 | 54.96 | +0.00 [+0.00, +0.00] | 0/1 | 0.000 | 0/1 | 1/1 | DL-01 x1 |
| ref:evidence | 1 | 54.96 |  | 1/1 | 1.000 | 0/1 | 1/1 | none |
| ref:always_accept | 1 | 54.96 | +0.00 [+0.00, +0.00] | 1/1 | 1.000 | 0/1 | 1/1 | none |
| ref:oracle | 1 | 54.96 | +0.00 [+0.00, +0.00] | 1/1 | 1.000 | 0/1 | 1/1 | none |

Stops: http_error 1.
Tokens: 0 in, 0 out.
