# 6. the gate adapter and how to wire it

Label: **engineering** (composes 01 to 04; no new mathematics).

Code: `scruple/stats/gate_adapter.py` (`evaluate_pairs`), public API in
`scruple/stats/__init__.py`. Tests: `tests/test_stats_gate_adapter.py`.
Evidence: `scripts/math_gate_demo.py`.

The gate engine (`scruple/gate/engine.py`) is owned by another workstream and is
**not** changed here. The adapter is a pure function: loss pairs in, verdict out.

## Contract

```python
from scruple.stats import LossPair, StatsGateConfig, evaluate_pairs

verdict = evaluate_pairs(pairs, StatsGateConfig(cost_cap=..., type_caps={...}))
verdict.outcome  # "promote" | "hold" | "block"
verdict.reasons  # human-readable, for the evidence report
verdict.regressed_fwer  # case types (and "__pooled__") flagged with FWER <= alpha_block
verdict.types  # per-type n, mean diff, e-values, time-uniform CS, flag
verdict.certified, verdict.certify_e_value, verdict.ci_low, verdict.ci_high
verdict.decision  # ComposedDecision when a prior is configured
```

Decision order: (1) **block** if the block family (one e-process per type for
`mean_k <= t_k`, plus a pooled one for `mean <= 0`, weighted e-Bonferroni on running
maxima) flags anything: `P(false block) <= alpha_block` (02, Proposition 2.2);
(2) **promote** if the pooled certificate for `mean < tolerance` reaches
`1/alpha_certify` and, when a prior is given, the cost-weighted Bayes rule agrees:
`P(promote | mean >= tolerance) <= alpha_certify` (04, Proposition 4.2);
(3) otherwise **hold**. e-BH and closed-testing flags are reported for information.

Inputs are validated: duplicate case ids, a pair exceeding its declared cap, per-type
caps above the global cap, and tolerances at or above caps are errors.

## Wiring into `evaluate_shadow` (for the engine owner)

Inside `evaluate_shadow`, after `b_out` and `c_out` are computed:

```python
from scruple.stats import LossPair, StatsGateConfig, evaluate_pairs

pairs = [
    LossPair(
        case_id=i,
        case_type=b_out.scenario[i].value,
        baseline_loss=b_out.regret[i],
        candidate_loss=c_out.regret[i],
    )
    for i in ids
]
sv = evaluate_pairs(pairs, cfg.stats)  # a new StatsGateConfig field on GateConfig
checks += (
    _check("stats_no_type_regression", "hard", not sv.regressed_fwer, "; ".join(sv.reasons)),
    _check("stats_certified_non_inferior", "evidence", sv.certified, ...),
)
```

Recommendations:

1. Start with both checks as `info` (report only), then promote the block check to
   `hard` once caps are agreed. It is the statistically controlled replacement for the
   zero-tolerance `no_slice_regression` check, whose false-block rate at the null
   boundary is about 0.73 to 1.0 in 02 (gap G-7).
2. Declare caps from observable facts **before** outcomes: amount plus fees and
   escalation cost. Stratify by scenario and amount band (`case_type=f"{s}/{band}"`,
   with `type_caps` per band) so small cases are not normalised by the largest cap.
3. Canary: feed realised costs of treatment and matched control as they resolve, into
   a `StratifiedMonitor` (time-uniform, stop at any time), and roll back as soon as
   `fwer_rejections` is non-empty. Canary arms are matched cohorts, not paired cases,
   so the per-case difference needs a pairing (for example, alternate treatment and
   control within a stratum in arrival order) or a two-sample e-process. Neither is
   implemented here; the paired test is exact for shadow replay.
4. Size the held-out set with `scruple.stats.plan` (03) instead of a fixed 3,000.
5. Store `verdict.model_dump()` in the evidence report; every number is reproducible.

## Results on the demo's held-out set (synthetic, seeded)

`uv run python scripts/math_gate_demo.py`. Same held-out generation as `evaluate_shadow`
(3,000 cases). Caps are declared from the generator's bounds (ASSUMPTION): amounts below
$10,000 (high value at most $50,000) plus all fees.

```
## good rule, strata = scenario: outcome=hold
n=3000, mean diff -1157.6 minor/case, pooled CS [-14913, +12598], certificate e=1.37, pooled regression e=1
  not_received             n= 876 cap=   10300$ mean  -3964.5 max e=       1 regressed=False
  (7 other types unchanged)
  reasons: not certified: e-value 1.37 < 20
## good rule, strata = scenario x amount band: outcome=hold
n=3000, mean diff -1157.6 minor/case, pooled CS [-14913, +12598], certificate e=1.37, pooled regression e=1
  not_received/<100        n= 525 cap=     400$ mean   -218.0 max e=    1.23 regressed=False
  not_received/<1000       n=  54 cap=    1300$ mean -19672.3 max e=       1 regressed=False
  not_received/<300        n= 295 cap=     600$ mean  -7783.5 max e=       1 regressed=False
  (22 other types unchanged)
  reasons: not certified: e-value 1.37 < 20
## bad rule A-1, strata = scenario: outcome=hold
n=3000, mean diff -1093.6 minor/case, pooled CS [-14849, +12662], certificate e=1.34, pooled regression e=1
  late_evidence            n= 178 cap=   10300$ mean  +1078.7 max e=    1.06 regressed=False
  not_received             n= 876 cap=   10300$ mean  -3964.5 max e=       1 regressed=False
  (6 other types unchanged)
  reasons: not certified: e-value 1.34 < 20
## bad rule A-1, strata = scenario x amount band: outcome=hold
n=3000, mean diff -1093.6 minor/case, pooled CS [-14849, +12662], certificate e=1.34, pooled regression e=1
  late_evidence/<100       n= 105 cap=     400$ mean  +1085.7 max e=    3.94 regressed=False
  late_evidence/<1000      n=  11 cap=    1300$ mean  +1090.9 max e=    1.01 regressed=False
  late_evidence/<300       n=  62 cap=     600$ mean  +1064.5 max e=    1.66 regressed=False
  not_received/<100        n= 525 cap=     400$ mean   -218.0 max e=    1.23 regressed=False
  not_received/<1000       n=  54 cap=    1300$ mean -19672.3 max e=       1 regressed=False
  not_received/<300        n= 295 cap=     600$ mean  -7783.5 max e=       1 regressed=False
  (19 other types unchanged)
  reasons: not certified: e-value 1.34 < 20

plan (bernstein): detect late_evidence regression of 10.79$/case (sd 6.76$, freq 0.059, cap 10300$) at alpha 0.05/9: 8677 late-evidence cases, 148755 held-out cases

plan (clt): detect late_evidence regression of 10.79$/case (sd 6.76$, freq 0.059, cap 10300$) at alpha 0.05/9: 5274 late-evidence cases, 90850 held-out cases
lower bound for any test valid over all laws on the cap: 3493 late-evidence cases (the demo has 178)
```

Reading, honestly:

- At 3,000 held-out cases and caps this loose, the adapter **holds** both the good rule
  and the bad rule A-1. It does not certify the good rule and does not flag A-1's
  late-evidence regression. The current gate blocks A-1 through its zero-tolerance slice
  check and policy constraint, which are correct here but have no error control.
- Stratifying by amount band tightens caps and raises the late-evidence e-value
  (1.06 to 3.94 in the smallest band) but not to the threshold.
- The planner and Proposition 3.3 explain why: detecting a $10.79-per-case regression
  under a $10,300 cap needs thousands of late-evidence cases (at least about 3,500 for
  any valid nonparametric test); the demo has 178. Either the held-out set grows
  (about 90,000 to 150,000 cases), or caps are justified much more tightly from domain
  facts (contesting a late-evidence case can only lose the $15 representment fee), or
  the hard per-case policy constraint remains the primary guard for this slice. All
  three are decisions for the policy owner, not for the statistics.
