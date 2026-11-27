# Suite scale check

`uv run python results/suite_scale/bench.py` on 4 CPUs, Python 3.12.3. Synthetic scenarios; public demo salt.

| step | result |
|---|---|
| plan 5000 scenarios, serial | 12.34 s (405/s) |
| plan 5000 scenarios, 3 workers | 7.94 s, identical plan hash |
| verify (regenerate every case, reveal every commitment) | 12.91 s, 0 problems |
| run scripted honest reference | 63.01 s (79.3/s) |
| run naive python agent | 54.18 s (92.3/s) |
| report (json, html, pdf) with baseline, all 5000 cases regenerated | 61.84 s |

plan_hash `23c9918df5a4cadf27f9d8869a14cf473e9fd18dc396494402dbeeec317d082b`  
bank_hash `f39de1426afccffb04a37353d7ac08bf1ca825d6822c05a02608b7e105647200`  
commitments_hash `9d9095513924120a2e540919fdf40ac681727d87462bd8192c6944a4dc8a7e2e`  
10 block hashes of 500 in scale.json. Mix: {'env': {'ach': 834, 'aml': 834, 'recon': 833, 'dispute': 833, 'commerce': 833, 'payables': 833}, 'band': {'medium': 2000, 'hard': 1500, 'easy': 1500}, 'adversarial': 2387, 'families': 109}.

Naive agent against the honest reference: {'verdict': 'BLOCK', 'gate_rate': 0.8352, 'reward_diff': -0.7153, 'reward_diff_ci': [-0.724, -0.7069], 'regenerated_and_revealed': 5000, 'problems': []}.
