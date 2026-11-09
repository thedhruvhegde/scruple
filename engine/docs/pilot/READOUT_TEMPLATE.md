# Pilot readout: `[PARTNER]`, card disputes

Template. Every `{{ ... }}` placeholder names the command and the field it is filled
from, so a number in the readout can be traced to a file and rerun. Every command named
here exists (checked 2026-11-09); what is still not built is listed in SCOPE.md.
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

From `scruple ingest <source> --in FILE --store cases.db --report I.json` (one report
per run, `scruple.ingest.pipeline.IngestReport`) and `scruple ingest verify --store
cases.db --json` (V, suppression-safe aggregates):

| field | value |
|---|---|
| source | `{{ I.source }}`; label `{{ I.label }}`; input sha256 `{{ I.input_sha256 }}` |
| window | `[first opened_at]` to `[last opened_at]` |
| records read | `{{ I.rows_read }}` |
| cases stored | `{{ I.cases }}` (`{{ I.cases_new }}` new, `{{ I.cases_updated }}` new versions) |
| records rejected | `{{ I.rows_failed }}`, by message: `{{ I.errors grouped by message }}` |
| skipped notifications (Adyen) | `{{ I.skipped }}` |
| reason-code mix | `{{ V.by_reason }}` (cells under k suppressed) |
| resolved share | `{{ V.by_state }}` |
| PII controls applied | `{{ I.redactions }}`, `{{ I.pans_tokenised }}` card tokens (`{{ V.token_key_ids }}`), `{{ I.fields_encrypted }}` encrypted fields (`{{ V.encrypted_fields }}`), card numbers in fields not kept `{{ I.pans_in_input_not_stored }}` |
| store check | `{{ V.raw_pans_found }}` card numbers in the file (must be 0), envelopes `{{ V.envelopes_checked }}` checked / `{{ V.envelopes_failed }}` failed |

Sample size check: `uv run python scripts/pilot_plan.py ... --json` with the signed
inputs. Paste `total_cases`, `months_of_history`, `shadow_weeks_traffic`,
`shadow_weeks_total` and compare with what arrived.

| | planned (signed) | available |
|---|---|---|
| resolved cases | `{{ PilotPlan.total_cases }}` | `[n]` |
| months of history | `{{ PilotPlan.months_of_history }}` | `[m]` |
| shadow weeks incl. lag | `{{ PilotPlan.shadow_weeks_total }}` | `[w]` |

## 4. Backtest on history

From `scruple backtest run --history cases.db --format store --candidate C --out R/`
(B = `R/report.json`, `BacktestReport`). Per reason code, from `B.by_reason[]`:

| reason code | cases | observed | estimated (changed decisions) | cost, history (observed) | candidate, observed part | candidate, estimated part (band) | verdict | cases needed |
|---|---|---|---|---|---|---|---|---|
| `[fraud]` | `{{ n }}` | `{{ n_observed }}` | `{{ n_estimated }}` | `{{ baseline_cost }}` | `{{ candidate_cost_observed }}` | `{{ candidate_cost_estimated }}` (`{{ candidate_cost_low }}` to `{{ candidate_cost_high }}`) | `{{ verdict }}` | `{{ plan.cases_needed }}` |

- Rule fields the export did not carry, and how many cases used a default:
  `{{ B.rule_fields_defaulted }}`.
- New hard violations on history (H2): from shadow mode on the imported cases (section
  7, `findings`), which runs the same checks on production's and the candidate's actions.

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
| data | `{{ S.data }}` ("synthetic", or "synthetic held-out cases and partner history" with `--history`) |
| sources | synthetic `{{ S.synthetic_verdict }}`; partner history `{{ S.history.verdict }}` (`{{ S.history.backtest_verdict }}`: `{{ S.history.why }}`), `{{ S.history.n_observed }}` observed / `{{ S.history.n_estimated }}` estimated, history content hash `{{ S.history.history_sha256 }}` |
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

## 7. Shadow mode

On the partner's cases: `POST /shadow/rules/{rule_id}/versions/{version}/imported/decisions`
(production's decisions on `ExternalCase`s), `.../imported/outcomes` as outcomes arrive,
and `GET .../imported` (`ImportedShadowSummary`; `docs/pilot/demo/shadow_imported.py`
does the same from a case store). Every metric has a `basis`:

| metric (`metrics[].name`) | basis | value |
|---|---|---|
| agreement | observed | `{{ agreement.value }}` over `{{ decisions }}` |
| changed decisions | observed | `{{ changed_decisions.value }}` |
| resolved so far | observed | `{{ resolved }}` of `{{ decisions }}` |
| production cost (resolved) | observed | `{{ production_cost.value }}` |
| candidate cost, same action | observed | `{{ candidate_cost_same_action.value }}` |
| candidate cost, changed action | estimated | `{{ candidate_cost_changed_action.value }}` (`{{ low }}` to `{{ high }}`), from `{{ profile }}` |
| findings | observed | `{{ findings[]: check, kind, production, candidate }}` |

On synthetic cases (the simulator, ground truth available), from
`GET /shadow/rules/{rule_id}/versions/{version}` (`ShadowSummary`):

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
| status at export | `{{ lifecycle.current_status }}` |
| gate verdict, when decided | `{{ lifecycle.gate_verdict }}` at `{{ lifecycle.gate_decision_id }}` (`{{ lifecycle.gate_stage }}`) |
| rollback | `{{ lifecycle.rolled_back }}` (actor, time, reason, restored version) or none |
| history | `{{ lifecycle.statement }}` |
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
