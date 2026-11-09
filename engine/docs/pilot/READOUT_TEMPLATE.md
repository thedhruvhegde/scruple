# Pilot readout: `[PARTNER]`, card disputes

Template. Every `{{ ... }}` placeholder names the command and the field it is filled
from, so a number in the readout can be traced to a file and rerun. Placeholders marked
**(planned)** come from tools that are part of the pilot build (SCOPE.md) and do not
exist yet; their field names are the intended output, to be confirmed when built.
Delete this paragraph in the delivered version.

| | |
|---|---|
| partner | `[PARTNER]` |
| period | `[start]` to `[end]` |
| scruple version | `{{ scruple version }}` (package version, commit, build date) |
| criteria document | SUCCESS_CRITERIA.md signed `[date]`, sha256 `[hash]` |
| prepared by / reviewed by | `[names, roles]` |

## 1. Summary

Three lines, no adjectives:

- Criteria passed: `[list of ids]`. Failed: `[list]`. Not enough data: `[list]`.
- The most important finding, with its number and the section it comes from.
- What the partner should do next, or "no recommendation".

## 2. Results against the pre-registered criteria

One row per criterion in the signed document, in its order. Do not add or drop rows.
Amended criteria show the original and the amendment.

| id | pass bar (as signed) | result | verdict | source |
|---|---|---|---|---|
| H1 | `[bar]` | `{{ planted candidates blocked }}` of `[K]` | pass / fail / not enough data | section 5 |
| H2 | `[bar]` | `{{ new hard violations: backtest + shadow }}` | | sections 4, 5 |
| H3 | `[bar]` | `{{ self-report gap per reason code, 95% interval }}` | | section 4 |
| H4 | `[bar]` | `{{ identical reruns }}` of `[3]` | | section 6 |
| H5 | `[bar]` | `{{ e-process result per reason code }}` | | section 4 |

## 3. Data received

From the ingest report **(planned: the ingest command; the fields below are
`scruple.ingest.tabular.ImportReport` and `scruple.ingest.adyen.AdyenResult`, which exist)**:

| field | value |
|---|---|
| source | Stripe / Adyen / CSV |
| window | `[first opened_at]` to `[last opened_at]` |
| rows read | `{{ ImportReport.rows_read }}` |
| cases imported | `{{ len(ImportReport.cases) }}` |
| rows rejected | `{{ ImportReport.rows_failed }}`, by message: `{{ ImportReport.errors grouped by message }}` |
| skipped notifications (Adyen) | `{{ AdyenResult.skipped grouped by reason }}` |
| reason-code mix | `{{ cases grouped by reason_code / source_reason }}` |
| resolved share | `{{ cases with state in won, lost, accepted, closed }}` |
| PII controls applied | redaction, tokenisation, encryption: which fields, which key ids (never the keys) |

Sample size check: `uv run python scripts/pilot_plan.py ... --json` with the signed
inputs. Paste `total_cases`, `months_of_history`, `shadow_weeks_traffic`,
`shadow_weeks_total` and compare with what arrived.

| | planned (signed) | available |
|---|---|---|
| resolved cases | `{{ PilotPlan.total_cases }}` | `[n]` |
| months of history | `{{ PilotPlan.months_of_history }}` | `[m]` |
| shadow weeks incl. lag | `{{ PilotPlan.shadow_weeks_total }}` | `[w]` |

## 4. Backtest on history (planned)

From the backtest report **(planned, ROADMAP 154)**. Per reason code:

| reason code | cases | decisions changed | new hard violations | cost on unchanged decisions (current) | cost on changed decisions (model-based range) |
|---|---|---|---|---|---|
| `[fraud]` | `{{ n }}` | `{{ changed }}` | `{{ hard }}` | `{{ realized cost }}` | `{{ low }}` to `{{ high }}` |

- Outcome model used for changed decisions: `[stated in week 1]`. These costs are
  model-based, not measured.
- Self-report gap (H3): partner-reported `[metric]` against the same metric from balance
  movements (`LedgerMovement` rows: `amount_minor`, `fee_minor`, `net_minor`), per reason
  code, with 95% intervals.
- H5, if registered: the stratified betting test from `scruple.stats` (docs/math/01 to
  03) on the paired differences, with the e-value and the decision per reason code.

## 5. Gate results (exists)

One block per gated version, from `scruple ci run --config scruple.toml --out S.json`
(fields of the JSON scorecard):

| field | value |
|---|---|
| candidate | `{{ S.rule_id }}` v`{{ S.version }}`, content hash `{{ S.content_hash }}` |
| logic | `{{ S.rule_logic }}` |
| verdict and exit code | `{{ S.verdict }}`, `{{ S.exit_code }}` |
| data | `{{ S.data }}` (today "synthetic"; with the backtest, the partner history window) |
| statistics | mean improvement `{{ S.statistics.mean_improvement_usd_minor }}` per case, 95% interval [`{{ S.statistics.ci_low }}`, `{{ S.statistics.ci_high }}`], `{{ S.statistics.n_cases }}` cases, `{{ S.statistics.affected_cases }}` changed, method `{{ S.statistics.method }}` |
| failed checks | `{{ S.checks where passed is false: name, detail }}` |
| regressions | `{{ S.regressions }}` |
| worst slice | `{{ S.slices with largest delta: scenario, n, delta, changed_cases }}` |
| canary | `{{ S.canary.treatment_mean_cost_usd_minor }}` vs `{{ S.canary.control_mean_cost_usd_minor }}` at `{{ S.canary.fraction }}` |
| config and audit | config sha256 `{{ S.config_sha256 }}`, audit head `{{ S.audit_head }}` |

Planted regressions (H1), one row each:

| plant | defect | expected reason code | verdict | flagged in | scorecard file |
|---|---|---|---|---|---|
| `[p1]` | `[e.g. override of a safeguard by priority]` | `[code]` | `{{ S.verdict }}` | `{{ S.slices / S.checks }}` | `[path]` |

## 6. Gate in the partner's CI (exists)

| field | value |
|---|---|
| repository and workflow | `[repo]`, `.github/workflows/scruple.yml` (or GitLab `scruple-gate` job) |
| required check | `scruple/gate` (commit status) or job check run |
| pull requests gated | `[n]`; verdicts: `[pass / hold / block counts]` |
| comment and status posted | from `scruple ci comment` and `scruple ci status --post` logs |
| reproducibility (H4) | `sha256sum` of the scorecard on `[3]` reruns: `[hashes]` |

State plainly whether the CI gate used synthetic held-out cases or the partner's
history.

## 7. Shadow mode (exists for synthetic cases; adapter planned)

From `GET /shadow/rules/{rule_id}/versions/{version}` (`ShadowSummary`):

| field | value |
|---|---|
| candidate | `{{ rule_ref }}`, `{{ content_hash }}` |
| decisions scored | `{{ n }}` |
| agreement with production | `{{ agree }}` of `{{ n }}` |
| hard findings, production vs candidate | `{{ production_hard_findings }}` vs `{{ candidate_hard_findings }}` |
| cost improvement interval | `{{ regret_improvement }}` (only where outcomes have resolved; see below) |
| data label | `{{ data }}` |

Per case type, from `by_case_type[]`: `case_type`, `n`, `agree`, `candidate_decided`,
`production_mean_cost_usd_minor`, `candidate_mean_cost_usd_minor`,
`production_hard_findings`, `candidate_hard_findings`.

Outcome status: `[x]` of `[n]` shadowed cases had resolved by `[date]`. Cost results for
the rest are open and are not reported as results.

## 8. Evidence packs (exists)

For each version that reached a decision, from
`scruple ci evidence --db gate.db RULE VERSION --out DIR` (JSON and PDF):

| field | value |
|---|---|
| pack | `evidence-{{ rule_id }}-v{{ version }}.json` / `.pdf`, sha256 `{{ pack_sha256 }}` |
| status at export | `{{ status }}` |
| decisions | `{{ decisions }}` |
| approvals | `{{ approvals.policy }}`, satisfied by `{{ approvals.satisfied }}` |
| seed commitments | `{{ seed_commitments }}` (seeds are not exported) |
| audit proof | `{{ audit }}` segment recomputes to the chain head: OK / FAILED |

And the whole log: `scruple audit verify --db gate.db --expected-head [anchored head]`
output pasted verbatim.

## 9. Limits of this pilot

Fill in every item that applies; do not delete the list.

- Which results are synthetic, which are model-based, which are measured.
- Reason codes below the planned sample size.
- Shadow cases still open at the readout date.
- Known verifier weaknesses that apply (docs/VERIFIER_FINDINGS.md G-1 to G-7).
- Policy constraints used, and who wrote them.
- Anything planned in SCOPE.md that was not finished, and what was used instead.

## 10. Data deletion

| item | deleted on | by | how |
|---|---|---|---|
| imported cases and ingest reports | | | |
| pilot database (SQLite file or Postgres schema) | | | |
| tokenisation and encryption keys held by scruple | | | |
| copies in backups or scratch locations | | | |

Signed: `[scruple]`, `[date]`. Acknowledged: `[partner]`, `[date]`.
