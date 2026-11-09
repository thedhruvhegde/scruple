# real models on simulated disputes

Seeds 5001-5012 (12 cases, one per seed), FULL_MIX engine.
Models played through the `scruple episode` CLI only; references replay the same seeds.
Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.

| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward | won/partial | fair outcome (OUT-01) | top hard failures |
|---|---|---|---|---|---|---|---|---|
| haiku | 12 | 64.21 | -4.90 [-20.96, +6.25] | 7/12 | 0.554 | 6/12 | 8/12 | PROC-01 x4, SR-01 x4, DL-01 x1 |
| sonnet | 12 | 36.34 | -32.77 [-64.01, -4.33] | 11/12 | 0.850 | 10/12 | 8/12 | DL-01 x1, PROC-01 x1, SR-01 x1 |
| opus | 12 | 71.20 | +2.08 [+0.00, +6.25] | 11/12 | 0.870 | 5/12 | 9/12 | SR-01 x1 |
| ref:evidence | 12 | 69.12 |  | 12/12 | 0.963 | 5/12 | 9/12 | none |
| ref:always_accept | 12 | 127.27 | +58.16 [+1.10, +144.54] | 12/12 | 0.880 | 0/12 | 6/12 | none |
| ref:oracle | 12 | 70.94 | +1.83 [+0.00, +5.48] | 12/12 | 0.958 | 4/12 | 10/12 | none |
