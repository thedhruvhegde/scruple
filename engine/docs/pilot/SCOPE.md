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
  memory cannot be expressed this way; replaying a real agent on history is **planned**
  (docs/DESIGN.md "Next", item 1) and would extend the pilot.
- Agree the export (DATA_CHECKLIST.md) and who runs it.
- scruple builds: an ingest command that reads a Stripe export, Adyen notifications or a
  CSV with an `ImportConfig`, and writes an ingest report. **Planned**: the parsers exist
  (`scruple/ingest/stripe`, `scruple/ingest/adyen.py`, `scruple/ingest/tabular.py`); the
  command line around them does not.

### Week 2: data export

- Partner runs the export in their environment.
- Ingest validates every row: unknown statuses or reasons are errors, not guesses; each
  rejected row is listed with its column and reason (`ImportReport`, **exists**).
- PII handling runs before anything is written (DATA_CHECKLIST.md). Redaction, card
  tokenisation and field encryption **exist** as library functions; wiring them into
  the ingest command is part of the week 1 build.
- Output: ingest report (rows read, rows imported, rows rejected by reason), signed off
  by the partner's owner.

### Week 3: backtest

- Replay the candidate and the current rules on the imported, resolved history.
- **Planned** (ROADMAP 154, "backtest: replay historical cases against a rule"). The
  gate today replays on simulator cases, which carry ground truth. Imported cases do
  not: for a case where the candidate would have acted differently, the real outcome
  of that other action is unknown. The backtest therefore reports, per reason code:
  - decisions changed (exact);
  - hard policy constraints broken (exact: they read observable fields only, see
    `scruple/verifiers/policy_constraints.py`). The three constraints in the code are
    synthetic examples (docs/ASSUMPTIONS.md A-6); the partner's own constraints are
    written down in week 1 and added as code, with tests, before the backtest runs;
  - cost on unchanged decisions (exact, from balance movements);
  - cost on changed decisions as a range or from a stated outcome model, labelled as
    model-based, never as measured.
- Planted regressions: copies of the candidate with a known defect (the priority
  override from the synthetic demo is the template) are gated on the same history to
  check that the gate catches them.
- Output: backtest report per reason code.

### Week 4: shadow mode

- The partner's production system keeps deciding. Each decision it makes is sent to
  scruple, which scores what the candidate would have done and returns statistics only.
- **Exists**: `AgentGate.shadow_recorder` and `POST /shadow/rules/{id}/versions/{v}/decisions`
  write nothing (tests count storage writes: zero). **Planned**: today the endpoint
  takes simulator cases (`DisputeCase`); an adapter from imported cases
  (`ExternalCase` plus the rule fields) is part of the build.
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
- Today the CI gate replays on synthetic held-out cases against the synthetic baseline.
  Gating against the partner's own history and current rules uses the week 3 backtest;
  if that is not finished, week 5 runs on synthetic data and the readout says so.
- Output: scorecards on real pull requests, one evidence pack per gated version.

### Week 6: readout

- Fill READOUT_TEMPLATE.md from the CLI outputs; review with the partner's reviewer.
- Result against each pre-registered criterion: pass, fail or not enough data.
- Deletion of partner data and keys, and a written confirmation (DATA_CHECKLIST.md).

## Roles

| role | partner side | scruple side |
|---|---|---|
| owner | one named person who signs the criteria and the readout | founder |
| engineer | runs the export, installs the CI action | builds the ingest command, backtest and shadow adapter |
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
- Replaying an LLM agent on history (planned; would extend the pilot).
- SSO, role-based access control, multi-tenancy, a web console (ROADMAP 157-158, 181-185;
  not built).
- Training any model on partner data. scruple does not.
- Legal, regulatory or network-rule advice.
