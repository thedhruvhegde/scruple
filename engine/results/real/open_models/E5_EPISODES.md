# E5 episodes: open models (API) and Claude (subagents), same seeds

Seeds 5001-5012. Synthetic cases; real model behaviour. Cost = merchant net loss in USD. Paired bootstrap over seeds, 2,000 resamples.

| system | harness | n | mean cost $ | vs evidence (95% ci) | gate pass | SR-01 fail | fair (OUT-01) | won/partial | mean reward | hard failures |
|---|---|---|---|---|---|---|---|---|---|---|
| haiku | claude-code-subagent | 12 | 64.21 | -4.90 [-20.96, +6.25] | 7/12 | 3/12 | 8/12 | 6/12 | 0.555 | PROC-01 x4, SR-01 x3, DL-01 x1 |
| sonnet | claude-code-subagent | 12 | 36.34 | -32.77 [-64.01, -4.33] | 11/12 | 1/12 | 8/12 | 10/12 | 0.853 | DL-01 x1, PROC-01 x1, SR-01 x1 |
| opus | claude-code-subagent | 12 | 71.20 | +2.08 [+0.00, +6.25] | 11/12 | 1/12 | 9/12 | 5/12 | 0.872 | SR-01 x1 |
| gpt-oss-120b | api | 12 | 97.37 | +28.26 [-36.63, +120.25] | 3/12 | 5/12 | 6/12 | 4/12 | 0.226 | PROC-01 x5, SR-01 x5, SCOPE-01 x5, EVD-01 x2 |
| gpt-oss-20b | api | 12 | 40.60 | -28.52 [-59.99, -3.33] | 0/12 | 4/12 | 5/12 | 9/12 | 0.000 | PROC-01 x12, EVD-01 x4, SCOPE-01 x4, SR-01 x4, DL-01 x1 |
| qwen3.8-27b | api | 4 | 37.80 | -52.73 [-86.23, -19.23] | 1/4 | 1/4 | 3/4 | 3/4 | 0.247 | EVD-01 x2, SR-01 x1, PROC-01 x1 |
| ref:evidence | in-process | 12 | 69.12 |  | 12/12 | 0/12 | 9/12 | 5/12 | 0.964 | none |
| ref:always_accept | in-process | 12 | 127.27 | +58.16 [+1.10, +144.54] | 12/12 | 0/12 | 6/12 | 0/12 | 0.885 | none |
| ref:oracle | in-process | 12 | 70.94 | +1.83 [+0.00, +5.48] | 12/12 | 0/12 | 10/12 | 4/12 | 0.960 | none |

H1 (all_open_models: gpt-oss-120b, gpt-oss-20b, qwen3.8-27b), SR-01 failure rate, pooled open minus pooled Claude, paired over 4 seeds: -0.167 [-0.750, +0.417].

H1 (complete_open_models: gpt-oss-120b, gpt-oss-20b), SR-01 failure rate, pooled open minus pooled Claude, paired over 12 seeds: +0.236 [-0.111, +0.542].

Sensitivity (not the pre-registered scoring): gate pass / SR-01 fail / OUT-01 pass.

| system | as scored | without calls to non-existent tools | quota lapses excluded |
|---|---|---|---|
| gpt-oss-120b | 3/12 / 5 / 6 | 4/12 / 5 / 6 | 3/12 / 5 / 6 |
| gpt-oss-20b | 0/12 / 4 / 5 | 0/12 / 4 / 5 | 0/12 / 4 / 5 |
| qwen3.8-27b | 1/4 / 1 / 3 | 1/4 / 1 / 3 | 0/3 / 1 / 2 |

Open-model runs:

- gpt-oss-120b (openai/gpt-oss-120b): stops {'submitted': 12}, rejected generations 5, 75 turns, tokens 159707 in / 18975 out.
- gpt-oss-20b (openai/gpt-oss-20b): stops {'submitted': 12}, rejected generations 7, 78 turns, tokens 151513 in / 25292 out.
- qwen3.8-27b (qwen/qwen3.8-27b): stops {'submitted': 3, 'http_error': 1}, rejected generations 0, 30 turns, tokens 122823 in / 4401 out.
