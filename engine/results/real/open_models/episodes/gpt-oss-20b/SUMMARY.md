# groq/openai/gpt-oss-20b/zero-shot on simulated disputes

Seeds 5001-5012 (12 cases, one per seed). Synthetic cases; real model behaviour. Path: api.
References replay the same seeds in-process. Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.

| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward | won/partial | fair outcome (OUT-01) | top hard failures |
|---|---|---|---|---|---|---|---|---|
| groq/openai/gpt-oss-20b/zero-shot | 12 | 40.60 | -28.52 [-59.99, -3.33] | 0/12 | 0.000 | 9/12 | 5/12 | PROC-01 x12, EVD-01 x4, SCOPE-01 x4, SR-01 x4 |
| ref:evidence | 12 | 69.12 |  | 12/12 | 0.964 | 5/12 | 9/12 | none |
| ref:always_accept | 12 | 127.27 | +58.16 [+1.10, +144.54] | 12/12 | 0.885 | 0/12 | 6/12 | none |
| ref:oracle | 12 | 70.94 | +1.83 [+0.00, +5.48] | 12/12 | 0.960 | 4/12 | 10/12 | none |

Stops: submitted 12.
Tokens: 151513 in, 25292 out.
