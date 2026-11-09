<!-- scruple-gate -->
## scruple gate: PASS

Candidate `inr-delivered-contest` version 1 (content hash `75110d71e7808b51`).

> Contest 'item not received' disputes when the carrier confirmed delivery.

Logic: `IF reason_code eq 'not_received' AND delivery_confirmed eq True THEN contest (priority 50)`

| Stage | Outcome | Decision | Summary |
|---|---|---|---|
| shadow | **promote** | `dec-00001` | Promoted to canary: improves outcomes with no regressions. |
| canary | **canary_passed** | `dec-00002` | Canary passed: ready for human approval. |

### Regressions

None found.

### Statistics

- Mean regret improvement: **$11.30** per case, 95% interval [$7.94, $14.43]
- 2,000 held-out cases, 345 changed decision; method `percentile-bootstrap-paired`

<details><summary>Per-scenario regret (minor units per case)</summary>

| Scenario | Cases | Baseline | Candidate | Change | Changed decisions |
|---|---:|---:|---:|---:|---:|
| currency_mismatch | 88 | 0.0 | 0.0 | +0.0 | 0 |
| duplicate_dispute | 65 | 0.0 | 0.0 | +0.0 | 0 |
| fraud | 596 | 1,964.2 | 1,964.2 | +0.0 | 0 |
| high_value | 54 | 3,500.0 | 3,500.0 | +0.0 | 0 |
| late_evidence | 115 | 0.0 | 0.0 | +0.0 | 0 |
| not_as_described | 399 | 5,374.8 | 5,374.8 | +0.0 | 0 |
| not_received | 579 | 7,347.8 | 3,443.1 | -3,904.7 | 345 |
| partial_refund | 104 | 0.0 | 0.0 | +0.0 | 0 |

</details>

### Canary

- 10% of simulated live traffic: treatment mean cost $17.47 vs control $118.27 (60 / 625 matched cases)

<details><summary>Checks (11/11 passed)</summary>

| Check | Type | Result | Detail |
|---|---|---|---|
| no_new_ledger_violations | hard | pass | 0 new ledger violations (e.g. over-refunds) |
| no_new_policy_violations | hard | pass | 0 new policy violations: none |
| golden_regression_suite | hard | pass | 0/10 golden cases regressed |
| no_slice_regression | hard | pass | worst slice 'currency_mismatch' mean regret change $0.00 per case (tolerance $0.00) |
| escalation_rate_guard | hard | pass | escalation rate 2.7% -> 2.7% |
| not_significantly_worse | hard | pass | mean improvement $11.30 per case, 95% CI [$7.94, $14.43] |
| enough_affected_cases | evidence | pass | 345 held-out cases changed decision (minimum 30) |
| significant_improvement | evidence | pass | CI lower bound $7.94 must exceed $0.00 per case |
| canary_no_hard_violations | hard | pass | 0 ledger/policy violations on canary cases |
| canary_cost_within_tolerance | hard | pass | treatment mean cost $17.47 vs control $118.27 (tolerance $5.00) |
| canary_enough_traffic | evidence | pass | 60 treatment / 625 control matched cases (minimum 20 each) |

</details>

Synthetic simulator data only. Thresholds are illustrative defaults. Config `b761d070feba74cf`, audit head `f33b999d755961fe`.
