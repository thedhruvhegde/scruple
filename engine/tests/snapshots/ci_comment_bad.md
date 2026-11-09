<!-- scruple-gate -->
## scruple gate: BLOCK

Candidate `inr-delivered-contest-fast` version 1 (content hash `92dc78fc4cfffcf2`).

> Always contest 'item not received' disputes with confirmed delivery; learned as a high-confidence override.

Logic: `IF reason_code eq 'not_received' AND delivery_confirmed eq True THEN contest (priority 5)`

| Stage | Outcome | Decision | Summary |
|---|---|---|---|
| shadow | **block** | `dec-00001` | Blocked: 3 hard check(s) failed. |

### Regressions

| Kind | Name | Detail |
|---|---|---|
| check | `no_new_policy_violations` | 80 new policy violations: no_contest_without_timely_evidence=80 |
| check | `golden_regression_suite` | 2/10 golden cases regressed: golden-late-evidence, golden-late-evidence-short-window |
| check | `no_slice_regression` | worst slice 'late_evidence' mean regret change $10.43 per case (tolerance $0.00) |
| slice | `late_evidence` | mean regret worse by $10.43 per case over 115 cases |
| golden | `golden-late-evidence` | golden case regressed |
| golden | `golden-late-evidence-short-window` | golden case regressed |

### Statistics

- Mean regret improvement: **$10.70** per case, 95% interval [$7.34, $14.30]
- 2,000 held-out cases, 425 changed decision; method `percentile-bootstrap-paired`

<details><summary>Per-scenario regret (minor units per case)</summary>

| Scenario | Cases | Baseline | Candidate | Change | Changed decisions |
|---|---:|---:|---:|---:|---:|
| currency_mismatch | 88 | 0.0 | 0.0 | +0.0 | 0 |
| duplicate_dispute | 65 | 0.0 | 0.0 | +0.0 | 0 |
| fraud | 596 | 1,964.2 | 1,964.2 | +0.0 | 0 |
| high_value | 54 | 3,500.0 | 3,500.0 | +0.0 | 0 |
| late_evidence | 115 | 0.0 | 1,043.5 | +1,043.5 **worse** | 80 |
| not_as_described | 399 | 5,374.8 | 5,374.8 | +0.0 | 0 |
| not_received | 579 | 7,347.8 | 3,443.1 | -3,904.7 | 345 |
| partial_refund | 104 | 0.0 | 0.0 | +0.0 | 0 |

</details>

<details><summary>Checks (5/8 passed)</summary>

| Check | Type | Result | Detail |
|---|---|---|---|
| no_new_ledger_violations | hard | pass | 0 new ledger violations (e.g. over-refunds) |
| no_new_policy_violations | hard | **FAIL** | 80 new policy violations: no_contest_without_timely_evidence=80 |
| golden_regression_suite | hard | **FAIL** | 2/10 golden cases regressed: golden-late-evidence, golden-late-evidence-short-window |
| no_slice_regression | hard | **FAIL** | worst slice 'late_evidence' mean regret change $10.43 per case (tolerance $0.00) |
| escalation_rate_guard | hard | pass | escalation rate 2.7% -> 2.7% |
| not_significantly_worse | hard | pass | mean improvement $10.70 per case, 95% CI [$7.34, $14.30] |
| enough_affected_cases | evidence | pass | 425 held-out cases changed decision (minimum 30) |
| significant_improvement | evidence | pass | CI lower bound $7.34 must exceed $0.00 per case |

</details>

Synthetic simulator data only. Thresholds are illustrative defaults. Config `da23d1db96ae3347`, audit head `d5b38bc934cc9325`.
