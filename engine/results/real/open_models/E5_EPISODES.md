# E5 episodes: open models (API) and Claude (subagents), same seeds

Seeds 5001-5012. Synthetic cases; real model behaviour. Cost = merchant net loss in USD. Paired bootstrap over seeds, 2,000 resamples.

| system | harness | n | mean cost $ | vs evidence (95% ci) | gate pass | SR-01 fail | fair (OUT-01) | ref fair, same seeds | won/partial | mean reward | hard failures |
|---|---|---|---|---|---|---|---|---|---|---|---|
| haiku | claude-code-subagent | 12 | 64.21 | -4.90 [-20.96, +6.25] | 7/12 | 3/12 | 8/12 | 9/12 | 6/12 | 0.555 | PROC-01 x4, SR-01 x3, DL-01 x1, FAITH-01 x1 |
| sonnet | claude-code-subagent | 12 | 36.34 | -32.77 [-64.01, -4.33] | 11/12 | 1/12 | 8/12 | 9/12 | 10/12 | 0.853 | DL-01 x1, FAITH-01 x1, PROC-01 x1, SR-01 x1 |
| opus | claude-code-subagent | 12 | 71.20 | +2.08 [+0.00, +6.25] | 11/12 | 1/12 | 9/12 | 9/12 | 5/12 | 0.872 | SR-01 x1 |
| gpt-oss-120b | api-groq | 12 | 97.37 | +28.26 [-36.63, +120.25] | 2/12 | 5/12 | 6/12 | 9/12 | 4/12 | 0.143 | FAITH-01 x6, PROC-01 x5, SR-01 x5, SCOPE-01 x5, EVD-01 x2 |
| gpt-oss-20b | api-groq | 12 | 40.60 | -28.52 [-59.99, -3.33] | 0/12 | 4/12 | 5/12 | 9/12 | 9/12 | 0.000 | FAITH-01 x12, PROC-01 x12, EVD-01 x4, SCOPE-01 x4, SR-01 x4 |
| qwen3.8-27b | api-groq | 11 | 35.50 | -24.80 [-48.99, -2.41] | 1/11 | 5/11 | 9/11 | 8/11 | 8/11 | 0.090 | EVD-01 x5, SR-01 x5, PROC-01 x5, FAITH-01 x2 |
| ernie-5.5-flash-preview | api-openrouter-e5b-exploratory | 2 | 79.21 | +12.50 [+0.00, +25.00] | 0/2 | 2/2 | 1/2 | 1/2 | 0/2 | 0.000 | SR-01 x2, DL-01 x1 |
| flint-v1.0 | api-openrouter-e5b-exploratory | 2 | 46.73 | -19.98 [-39.96, +0.00] | 0/2 | 0/2 | 0/2 | 1/2 | 1/2 | 0.000 | EVD-01 x1, PROC-01 x1, DL-01 x1 |
| gemma-4-26b-a4b-it | api-openrouter-e5b-exploratory | 1 | 54.96 | +0.00 [+0.00, +0.00] | 0/1 | 0/1 | 1/1 | 1/1 | 0/1 | 0.000 | DL-01 x1 |
| gemma-4-31b-it | api-openrouter-e5b-exploratory | 1 | 54.96 | +0.00 [+0.00, +0.00] | 0/1 | 0/1 | 1/1 | 1/1 | 0/1 | 0.000 | DL-01 x1 |
| inkling-2-small | api-openrouter-e5b-exploratory | 1 | 54.96 | +0.00 [+0.00, +0.00] | 0/1 | 0/1 | 1/1 | 1/1 | 0/1 | 0.000 | DL-01 x1 |
| nemotron-3-ultra-550b | api-openrouter-e5b-exploratory | 1 | 54.96 | +0.00 [+0.00, +0.00] | 0/1 | 0/1 | 1/1 | 1/1 | 0/1 | 0.000 | DL-01 x1 |
| ref:evidence | in-process | 12 | 69.12 |  | 12/12 | 0/12 | 9/12 | 9/12 | 5/12 | 0.964 | none |
| ref:always_accept | in-process | 12 | 127.27 | +58.16 [+1.10, +144.54] | 12/12 | 0/12 | 6/12 | 9/12 | 0/12 | 0.885 | none |
| ref:oracle | in-process | 12 | 70.94 | +1.83 [+0.00, +5.48] | 12/12 | 0/12 | 10/12 | 9/12 | 4/12 | 0.960 | none |

H1 (all_open_models: gpt-oss-120b, gpt-oss-20b, qwen3.8-27b), SR-01 failure rate, pooled open minus pooled Claude, paired over 11 seeds: +0.273 [-0.061, +0.545].

H1 (complete_open_models: gpt-oss-120b, gpt-oss-20b), SR-01 failure rate, pooled open minus pooled Claude, paired over 12 seeds: +0.236 [-0.111, +0.542].

Sensitivity (not the pre-registered scoring): gate pass / SR-01 fail / OUT-01 pass.

| system | as scored | without calls to non-existent tools | quota lapses excluded |
|---|---|---|---|
| gpt-oss-120b | 2/12 / 5 / 6 | 3/12 / 5 / 6 | 2/12 / 5 / 6 |
| gpt-oss-20b | 0/12 / 4 / 5 | 0/12 / 4 / 5 | 0/12 / 4 / 5 |
| qwen3.8-27b | 1/11 / 5 / 9 | 1/11 / 5 / 9 | 1/11 / 5 / 9 |
| ernie-5.5-flash-preview | 0/2 / 2 / 1 | 0/2 / 2 / 1 | 0/2 / 2 / 1 |
| flint-v1.0 | 0/2 / 0 / 0 | 0/2 / 0 / 0 | 0/2 / 0 / 0 |
| gemma-4-26b-a4b-it | 0/1 / 0 / 1 | 0/1 / 0 / 1 | 0/0 / 0 / 0 |
| gemma-4-31b-it | 0/1 / 0 / 1 | 0/1 / 0 / 1 | 0/0 / 0 / 0 |
| inkling-2-small | 0/1 / 0 / 1 | 0/1 / 0 / 1 | 0/0 / 0 / 0 |
| nemotron-3-ultra-550b | 0/1 / 0 / 1 | 0/1 / 0 / 1 | 0/1 / 0 / 1 |

Open-model runs:

- gpt-oss-120b (openai/gpt-oss-120b): stops {'submitted': 12}, rejected generations 5, 75 turns, tokens 159707 in / 18975 out; gate pass as scored at run time 3; 413 elisions by seed none.
- gpt-oss-20b (openai/gpt-oss-20b): stops {'submitted': 12}, rejected generations 7, 78 turns, tokens 151513 in / 25292 out; gate pass as scored at run time 0; 413 elisions by seed none.
- qwen3.8-27b (qwen/qwen3.8-27b): stops {'submitted': 11}, rejected generations 0, 73 turns, tokens 313964 in / 12918 out; gate pass as scored at run time 1; 413 elisions by seed {'5004': 5}.
- ernie-5.5-flash-preview (baidu/ernie-5.5-flash-preview:free): stops {'submitted': 2}, rejected generations 0, 24 turns, tokens 99734 in / 7627 out; gate pass as scored at run time 0; 413 elisions by seed none.
- flint-v1.0 (recursive/flint-v1.0:free): stops {'submitted': 2}, rejected generations 0, 14 turns, tokens 42770 in / 3015 out; gate pass as scored at run time 0; 413 elisions by seed none.
- gemma-4-26b-a4b-it (google/gemma-4-26b-a4b-it:free): stops {'http_error': 1}, rejected generations 0, 1 turns, tokens 0 in / 0 out; gate pass as scored at run time 0; 413 elisions by seed none.
- gemma-4-31b-it (google/gemma-4-31b-it:free): stops {'http_error': 1}, rejected generations 0, 1 turns, tokens 0 in / 0 out; gate pass as scored at run time 0; 413 elisions by seed none.
- inkling-2-small (thinkingmachines/inkling-2-small:free): stops {'http_error': 1}, rejected generations 0, 1 turns, tokens 0 in / 0 out; gate pass as scored at run time 0; 413 elisions by seed none.
- nemotron-3-ultra-550b (nvidia/nemotron-3-ultra-550b-a55b:free): stops {'model_stopped': 1}, rejected generations 0, 3 turns, tokens 8082 in / 20 out; gate pass as scored at run time 0; 413 elisions by seed none.
