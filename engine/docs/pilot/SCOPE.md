# Pilot scope: six weeks, one workflow (card disputes)

One partner, one dispute operation, one candidate change. Each week names what is used
as it exists today (**exists**, with the code path) and what scruple builds during the
pilot (**planned**, with the roadmap item it comes from). Nothing planned is promised
as finished until it is merged with tests.

## Before week 1

- Mutual NDA and pilot terms signed (TERMS_DRAFT.md, after counsel review).
- Partner fills in the planner inputs: monthly dispute volume, reason-code mix, smallest
  per-case improvement worth detecting, a per-case cost cap, and the median time from
  dispute opened to final outcome (`scripts/pilot_plan.py`, SUCCESS_CRITERIA.md).
- Deployment choice: scruple runs inside the partner's environment (Docker image) or on
  a scruple-operated machine with only tokenised, redacted data. See SECURITY.md.

## Week by week

### Week 1: kickoff

- Agree and sign the pre-registered success criteria (SUCCESS_CRITERIA.md). Nothing is
  loaded before this is signed.
- Walk through the partner's current rules or agent version, and the candidate change.
- Translate the current rules and the candidate into scruple's rule language
  (`scruple.domain.RuleSpec`: conditions over whitelisted fields, action accept,
  contest or escalate). **Exists** for structured rules. An LLM agent or natural-language
  memory cannot be expressed this way. The backtest also takes a Python module whose
  `decide(view)` returns an action (`--candidate agent.py`, docs/BACKTEST.md); wrapping
  a real LLM agent that way has not been tried, so replaying one on history is still
  **planned** (docs/DESIGN.md "Next", item 1) and would extend the pilot.
- Agree the export (DATA_CHECKLIST.md) and who runs it.
- The ingest command **exists**: `scruple ingest stripe|adyen|csv|jsonl --in FILE
  --store cases.db --report ingest.json` validates every record, redacts, tokenises card
  numbers, encrypts chosen fields, stores the result in an append-only case store and
  writes a row-level report; `scruple ingest verify --store cases.db` summarises the
  store with small-cell suppression (docs/INTEGRATIONS.md). Week 1 work is the
  partner's `ImportConfig` (for CSV) and the choice of columns to tokenise or encrypt.

### Week 2: data export

- Partner runs the export in their environment.
- Ingest validates every row: unknown statuses or reasons are errors, not guesses; each
  rejected row is listed with its column and reason (`ImportReport`, **exists**).
- PII handling runs before anything is written (DATA_CHECKLIST.md): redaction, keyed
  card tokens and AES-256-GCM field encryption are applied by `scruple ingest`
  (**exists**), keys from the partner's environment only. The store refuses any record
  whose clear text still holds a card number.
- Output: ingest report (records read, stored, rejected with row, column and message)
  and the `ingest verify` summary, signed off by the partner's owner.

### Week 3: backtest

- Replay the candidate and the current rules on the imported, resolved history.
- **Exists** (ROADMAP 154): `scruple backtest run --history cases.db --format store
  --candidate rule.json --out report/` (docs/BACKTEST.md). Imported cases carry no
  ground truth: for a case where the candidate would have acted differently, the real
  outcome of that other action is unknown. The backtest therefore reports, per reason
  code:
  - decisions changed (exact);
  - cost on unchanged decisions (observed, from balance movements);
  - cost on changed decisions, estimated from the calibrated profile with an
    optimistic and a pessimistic end, labelled estimated, never as measured;
  - which rule fields the export lacked and were defaulted, per case count.
- Hard policy constraints on history: shadow mode on the imported cases (week 4 tooling,
  runnable on history in week 3) checks deadlines and ledger consistency on both
  production's and the candidate's actions. The deadline check mirrors a synthetic
  constraint (docs/ASSUMPTIONS.md A-6); the partner's own constraints are written down
  in week 1 and added as `extra_checks`, with tests, before the backtest runs.
- Planted regressions: copies of the candidate with a known defect (the priority
  override from the synthetic demo is the template) are gated on the same history to
  check that the gate catches them.
- Output: backtest report per reason code.

### Week 4: shadow mode

- The partner's production system keeps deciding. Each decision it makes is sent to
  scruple, which scores what the candidate would have done and returns statistics only.
- **Exists**: `AgentGate.shadow_recorder` and its routes write nothing (tests count
  storage writes: zero, and no files). For the partner's real cases:
  `POST /shadow/rules/{id}/versions/{v}/imported/decisions` takes production's decisions
  on `ExternalCase`s, `.../imported/outcomes` takes the resolved versions when they
  arrive, `GET .../imported` returns the summary. Every metric is labelled observed
  (agreement, changed decisions, findings, realised cost) or estimated (cost of a
  changed decision). `docs/pilot/demo/shadow_imported.py` runs the same from a store.
- Not built: the recorder lives in the API process's memory; a restart loses it, and
  decisions are not persisted (by design they never touch storage). Re-send the batch.
- Outcomes lag. Decisions shadowed in week 4 mostly resolve after week 6; the planner's
  `--outcome-lag-weeks` says by how much. The readout reports agreement and constraint
  results from week 4 and states which cost results are still open.
- Output: shadow summary.

### Week 5: gate on the partner's CI

- Install the GitHub Action (`.github/actions/scruple-gate`) or the GitLab template
  (`.gitlab/scruple-gate.yml`) on one repository the partner chooses. **Exists**; it has
  been tested locally and through a fake HTTP transport, not yet on github.com or
  gitlab.com (docs/CI.md).
- Make `scruple/gate` a required status check (docs/CI.md has the steps).
- `scruple ci run --history cases.db` (**exists**) gates the candidate on the synthetic
  held-out cases and on the partner's history (the week 3 backtest), and the scorecard
  and PR comment show both sources separately with their own verdicts; the overall
  verdict is the worse one. A history verdict of "insufficient evidence" is a HOLD.
  The CI job needs the case store (or the export) inside the partner's runner, and the
  history side judges the candidate rule alone, not the partner's full current rule set.
- Output: scorecards on real pull requests, one evidence pack per gated version.

### Week 6: readout

- Fill READOUT_TEMPLATE.md from the CLI outputs; review with the partner's reviewer.
- Result against each pre-registered criterion: pass, fail or not enough data.
- Deletion of partner data and keys, and a written confirmation (DATA_CHECKLIST.md).

## Roles

| role | partner side | scruple side |
|---|---|---|
| owner | one named person who signs the criteria and the readout | founder |
| engineer | runs the export, installs the CI action | writes the `ImportConfig`, the partner's constraints as checks, and runs ingest, backtest and shadow |
| dispute expert | explains current rules and reason-code handling | n/a |
| reviewer | risk, compliance or dispute lead who reads the evidence packs | n/a |
| approver | a named person, not the rule's owner, who may approve activation | n/a |

Separation of duties is enforced by the gate: the rule's owner and the learning agent
can never approve (`scruple/gate/approval.py`, tests in `tests/test_gate_approval.py`).

## Hours asked of the partner

Estimates for planning, not measurements; adjusted at kickoff.

| week | owner | engineer | dispute expert | reviewer |
|---|---|---|---|---|
| 1 | 4 | 2 | 3 | 0 |
| 2 | 2 | 6 | 1 | 0 |
| 3 | 3 | 1 | 2 | 1 |
| 4 | 2 | 2 | 1 | 0 |
| 5 | 2 | 6 | 0 | 0 |
| 6 | 3 | 0 | 1 | 3 |
| total | 16 | 17 | 8 | 4 |

## Exit criteria

The pilot ends early, by either side, with a short written note, if:

- the export cannot be produced in the agreed format by the end of week 2;
- more than `[X]%` of exported rows are rejected by ingest for reasons that cannot be
  fixed by a mapping change (the threshold is set at kickoff);
- the planner shows the agreed hypotheses cannot reach their sample size from the
  available history and traffic, and no smaller hypothesis is agreed instead;
- a security or data-handling condition in SECURITY.md or the terms is not met.

The pilot ends normally at the week 6 readout. A failed criterion is not an exit
criterion: it is a result, and it is reported.

## Out of scope

- Workflows other than card disputes (ACH, wires, reconciliation, AML, payables).
- Writing to Stripe, Adyen or any network: no evidence submission, no case changes.
  The Stripe client is read-only (`GET` only, docs/INTEGRATIONS.md).
- Acting on live traffic. Shadow mode acts on nothing. A real canary (routing live
  disputes to the candidate) is not part of the six weeks; it needs the partner's
  approval, a delayed-outcome canary that is not built (docs/math/README.md), and more
  time than outcome lag allows.
- Replaying an LLM agent on history (planned; would extend the pilot). Python agents
  with a `decide(view)` function can already be backtested.
- SSO, role-based access control, multi-tenancy, a web console (ROADMAP 157-158, 181-185;
  not built).
- Training any model on partner data. scruple does not.
- Legal, regulatory or network-rule advice.
