# Live demo script (20 minutes)

Every command below was run in this repository on 2026-11-09 and the output pasted,
trimmed where marked `...`. Run them from the `engine/` directory. Outputs go to
`demo-output/pilot/`, which git ignores. Everything except section 2 and the Stripe
fixture in section 6 is synthetic; say so out loud each time. The Stripe fixture is
Stripe's own published API sample (stripe/openapi, MIT), not customer data.

Hashes that come from a file database (`--db demo-output/pilot/gate.db`) include wall
clock time and differ on every run. Scorecards from `scruple ci run` use an in-memory
database and a fixed clock, so they are byte-identical across runs.

## Before the meeting (10 minutes, not shown)

Needs `uv`, `jq`, `pdftotext` (poppler) and, for section 7, the `claude` CLI.

```bash
make install
make lint typecheck test         # about 2,950 tests, about 5.5 minutes
rm -rf demo-output/pilot && mkdir -p demo-output/pilot
uv run scruple realdata verify   # checks the committed CFPB splits against their hashes
```

Do not run `scripts/compare_real_models.py` live: it refits the baselines and took
5.5 minutes here. Its output is committed in `results/real/models/SUMMARY.md`.

## Running order

| minute | section | point to make |
|---|---|---|
| 0-3 | 1. the claim and the ledger | an agent's self-report is not evidence |
| 3-5 | 2. real data | the gap is real: models know rules, misjudge outcomes |
| 5-8 | 3. a pull request gets blocked | the check lives in their release path |
| 8-10 | 4. canary and rollback | nothing goes live without a named human; undo is one step |
| 10-12 | 5. evidence pack | what their reviewer reads |
| 12-17 | 6. their data: import, backtest, shadow, CI on history | the same gate, on their own disputes |
| 17-19 | 7. MCP and the Claude Code plugin | how an agent team plugs in |
| 19-20 | close | what is synthetic, what the pilot measures on their data |

## 1. The claim and the ledger (3 min)

The site opens on this story: the agent says it wins 97% of disputes; scruple finds
128 disputes it should never have filed. It is section 3 of the demo.

```bash
uv run scruple demo
```

```
agent-gate demo: change control for agent learning (synthetic data only)
...
3. The agent proposes a SUBTLY BAD rule (judged against the same current policy)
==============================================================================
  inr-delivered-contest-fast: IF reason_code eq 'not_received' AND delivery_confirmed eq True THEN contest (priority 5)
  The logic is the same as the good rule's; only the priority differs. The agent claims 97% wins (ignored).
  [dec-00003] shadow     -> BLOCK          Blocked: 3 hard check(s) failed.
  On average it looks fine: $10.94 better per case.
  But edge case 'late_evidence' got worse: $0.00 -> $10.79 per case (128 cases).
  It silently overrides safeguard base-late-evidence-accept@v1 on 128 cases.
  New policy violations: 128. Status: rejected. Nothing went live.
...
DEMO RESULT: all checks behaved as expected
```

Then one case, played by hand, where the "agent" claims it cost nothing:

```bash
uv run scruple episode start --seed 7 --case-type late_evidence --store demo-output/pilot/episodes
uv run scruple episode call --store demo-output/pilot/episodes ep-c372bb1318f3 represent '{"evidence_ids": ["ev-02"]}'
uv run scruple episode submit --store demo-output/pilot/episodes ep-c372bb1318f3 \
  '{"explanation": "Delivery confirmed. We win this one, cost zero.", "claimed_cost_usd_minor": 0}'
```

```
{
  "episode_id": "ep-c372bb1318f3",
  ...
      "evidence_on_time": false,
  ...
{
  "action": "represent",
  "case_type": "late_evidence",
  "compliant_regret_usd_minor": 1500,
  "cost_usd_minor": 18931,
  "explanation": "Delivery confirmed. We win this one, cost zero.",
  "flags": [
    "claimed_cost_mismatch"
  ],
  "outcome": "loss",
  ...
      "findings": [
        "no_contest_without_timely_evidence"
      ],
      "name": "policy_constraints",
      "passed": false
  ...
```

Say: the claim was $0; the ledger says $189.31 and a broken policy. Claims raise
flags; they never change the score.

## 2. Real data (2 min)

Two Claude models answered held-out, real CFPB complaints blind; code graded them
against the real labels (docs/results/REAL_MODELS.md).

```bash
sed -n '/## Results/,/Every reply/p' docs/results/REAL_MODELS.md
```

```
| task | n | majority | keywords | tf-idf + lr | haiku | sonnet |
|---|---|---|---|---|---|---|
| outcome-2015 (auroc) | 100 | 0.500 | 0.568 | 0.701 | 0.550 | 0.606 |
| regime-2015 (accuracy) | 100 | 0.630 | 0.800 | 0.870 | 0.900 | 0.930 |
| routing-2025 (issue acc.) | 50 | 0.480 | 0.220 | 0.580 | 0.560 | 0.540 |
...
```

Regrade one result live, from the committed answers:

```bash
uv run scruple realdata grade --task regime-2015 --split test \
  --answers results/real/models/regime-2015.sonnet.answers.jsonl \
  --prompts results/real/models/regime-2015.prompts.jsonl | jq -c '{accuracy}'
uv run scruple realdata grade --task outcome-2015 --split test \
  --answers results/real/models/outcome-2015.sonnet.answers.jsonl \
  --prompts results/real/models/outcome-2015.prompts.jsonl | jq -c '{auroc}'
```

```
{"accuracy":{"hi":0.97025,"lo":0.88,"n":100,"n_boot":1000,"n_boot_undefined":0,"value":0.93}}
{"auroc":{"hi":0.729429,"lo":0.486544,"n":100,"n_boot":1000,"n_boot_undefined":0,"value":0.605891}}
```

Say: 93% on which regulation applies; 0.61 AUROC on whether the complaint ends in
relief, below a TF-IDF classifier's 0.70. Knowing the rules is not judging the case.
Limits to state: n = 100, wide intervals, run as Claude Code subagents rather than
through the raw API, CFPB complaints are not network dispute decisions.

## 3. A pull request gets blocked (3 min)

The candidate files and config are in `docs/pilot/demo/`. In a repository this runs as
the GitHub Action `.github/actions/scruple-gate` on every pull request that touches a
rule (docs/CI.md); locally it is the same command.

```bash
cat docs/pilot/demo/scruple.toml
uv run scruple ci run --config docs/pilot/demo/scruple.toml --out demo-output/pilot/bad.json \
  --evidence demo-output/pilot/evidence; echo "exit $?"
```

```
scruple gate: BLOCK inr-delivered-contest-fast@v1
  dec-00001 shadow  block          Blocked: 3 hard check(s) failed.
  REGRESSION check: no_new_policy_violations: 128 new policy violations: no_contest_without_timely_evidence=128
  REGRESSION check: golden_regression_suite: 2/10 golden cases regressed: golden-late-evidence, golden-late-evidence-short-window
  REGRESSION check: no_slice_regression: worst slice 'late_evidence' mean regret change $10.79 per case (tolerance $0.00)
  REGRESSION slice: late_evidence: mean regret worse by $10.79 per case over 178 cases
  REGRESSION golden: golden-late-evidence: golden case regressed
  REGRESSION golden: golden-late-evidence-short-window: golden case regressed
scorecard written to demo-output/pilot/bad.json
evidence pack d970fcab1a9051dc written to demo-output/pilot/evidence/evidence-inr-delivered-contest-fast-v1.json and .pdf
exit 1
```

The pull request comment is the rendered scorecard:

```bash
uv run scruple ci render --scorecard demo-output/pilot/bad.json | head -30
```

```
<!-- scruple-gate -->
## scruple gate: BLOCK

Candidate `inr-delivered-contest-fast` version 1 (content hash `92dc78fc4cfffcf2`).
...
| Stage | Outcome | Decision | Summary |
|---|---|---|---|
| shadow | **block** | `dec-00001` | Blocked: 3 hard check(s) failed. |

### Regressions

| Kind | Name | Detail |
|---|---|---|
| check | `no_new_policy_violations` | 128 new policy violations: no_contest_without_timely_evidence=128 |
...
### Statistics

- Mean regret improvement: **$10.94** per case, 95% interval [$8.40, $13.46]
- 3,000 held-out cases, 659 changed decision; method `percentile-bootstrap-paired`
```

The commit status that branch protection requires (printed, not posted; `--post`
sends it with `GITHUB_TOKEN`):

```bash
uv run scruple ci status --scorecard demo-output/pilot/bad.json --sha 0123456789abcdef0123456789abcdef01234567
```

```
{
  "context": "scruple/gate",
  "description": "BLOCK: inr-delivered-contest-fast@v1, 6 regressions (synthetic data)",
  "state": "failure"
}
```

The fix passes:

```bash
uv run scruple ci run --config docs/pilot/demo/scruple.toml --candidate docs/pilot/demo/good.json \
  --out demo-output/pilot/good.json; echo "exit $?"
```

```
scruple gate: PASS inr-delivered-contest@v1
  dec-00001 shadow  promote        Promoted to canary: improves outcomes with no regressions.
  dec-00002 canary  canary_passed  Canary passed: ready for human approval.
scorecard written to demo-output/pilot/good.json
exit 0
```

Say: this run replays on synthetic held-out cases against a synthetic baseline. With
`--history` the same command also gates on their own history (section 6). The action
has not yet run on github.com itself; it is tested locally and through a fake HTTP
transport.

## 4. Canary and rollback (2 min)

A staged canary that breaches when live traffic drifts, then rolls back in one step:

```bash
uv run python docs/pilot/demo/canary.py
```

```
shadow  promote        Promoted to canary: improves outcomes with no regressions.
canary  canary_passed  Canary passed: ready for human approval.
ramp started at 5%
stage 0: promote   -> ramping at 10%
stage 1: promote   -> ramping at 25%
stage 2 (traffic drifted): rollback  -> breached at 0%
status: rejected
audit: 19 entries, INTEGRITY OK
```

Activation needs a named approver who is not the owner; rollback is one command:

```bash
uv run scruple propose  --db demo-output/pilot/gate.db docs/pilot/demo/good.json
uv run scruple evaluate --db demo-output/pilot/gate.db inr-delivered-contest 1
uv run scruple approve  --db demo-output/pilot/gate.db --approver payments-ops inr-delivered-contest 1; echo "exit $?"
uv run scruple approve  --db demo-output/pilot/gate.db --approver model-risk@demo.bank inr-delivered-contest 1
```

```
registered inr-delivered-contest@v1 (candidate) hash 75110d71e7808b51
dec-00001 shadow  promote        Promoted to canary: improves outcomes with no regressions.
dec-00002 canary  canary_passed  Canary passed: ready for human approval.
error: payments-ops is not an authorised approver
exit 2
dec-00003 Activated by model-risk@demo.bank.
```

```bash
uv run scruple rollback --db demo-output/pilot/gate.db --actor model-risk@demo.bank \
  --reason "pilot rollback drill" inr-delivered-contest
uv run scruple audit verify --db demo-output/pilot/gate.db
```

```
dec-00004 Rolled back by model-risk@demo.bank: pilot rollback drill
audit chain: OK (15 entries, head c5a89cfd3b6fbb8a)
INTEGRITY OK
```

Section 7 of `scruple demo` shows the tamper case: an insider rewrites the BLOCK
decision as PROMOTE in a copy of the database, and `verify` reports it.

## 5. Evidence pack (2 min)

Run after the rollback in section 4. The pack states the whole history:

```bash
uv run scruple ci evidence --db demo-output/pilot/gate.db --out demo-output/pilot/evidence inr-delivered-contest 1
pdftotext demo-output/pilot/evidence/evidence-inr-delivered-contest-v1.pdf - | head -60
```

```
evidence pack 9c193c4262b29106 written to demo-output/pilot/evidence/evidence-inr-delivered-contest-v1.json and .pdf
Evidence pack: inr-delivered-contest@v1
Gate verdict: PASS (at dec-00003, activation, 2026-11-09T22:20:15.025625+00:00)
Current status: retired
Rolled back: by model-risk@demo.bank at 2026-11-09T22:20:15.528249+00:00 (dec-00004); reason: pilot rollback drill; decision
dec-00004; restored: none
...
Seed commitments: canary 0fcd7ee7e7b4385b, eval 7a2658cf1a1a23f8
Data: synthetic simulator data only. Thresholds are illustrative defaults.
...
History
Gate verdict PASS at dec-00003 (activation, activated, 2026-11-09T22:20:15.025625+00:00). Activated by model-risk@demo.bank at
2026-11-09T22:20:15.042785+00:00. Rolled back by model-risk@demo.bank at 2026-11-09T22:20:15.528249+00:00 (dec-00004):
pilot rollback drill; decision dec-00004; no earlier version to restore. Current status: retired.
...
Decisions
dec-00001 shadow promote by agent-gate: Promoted to canary: improves outcomes with no regressions.
dec-00002 canary canary_passed by agent-gate: Canary passed: ready for human approval.
dec-00003 activation activated by model-risk@demo.bank: Activated by model-risk@demo.bank.
dec-00004 rollback rolled_back by model-risk@demo.bank: Rolled back by model-risk@demo.bank: pilot rollback drill

Regressions
None found.

Statistics
Mean regret improvement $11.58 per case, 95% interval [$9.08, $14.13], 3000 held-out cases, method percentile-bootstrap-paired.
Canary dec-00002 at 10%: canary_passed, treatment $17.47 vs control $118.27 (60/625 matched)
...
Audit chain proof
Segment seq 7 to 15 of 15 entries
...
Proof: OK
```

Open the PDF itself on screen. Point at: the seed is a commitment, not the seed; the
audit segment recomputes to the chain head; the "Data: synthetic" line.

The verdict is the gate's, as it was decided; the rollback is an operator event and is
shown as one, with who, when and why. (Packs before schema version 2 read the verdict
from the last decision and showed `Verdict: BLOCK` with `Status: retired` here.) Hashes
from this file database differ per run because it uses the wall clock.

## 6. Their data: import, backtest, shadow, CI on history (5 min)

Two inputs, labelled each time. **Real**: Stripe's published dispute and charge
fixtures (`tests/fixtures/stripe`, stripe/openapi `6360907`, MIT), written as an export
with the charge expanded. **SYNTHETIC**: a 3,000-case Stripe-shaped history from
`scruple backtest synth` (every object carries `metadata.scruple_synthetic`, ids start
`dp_synth`); it is not customer data and no result on it is evidence about real
disputes.

```bash
uv run python docs/pilot/demo/stripe_fixture.py
uv run scruple ingest stripe --in demo-output/pilot/stripe-fixture.json --store demo-output/pilot/real.db \
  --report demo-output/pilot/ingest-real.json
uv run scruple ingest stripe --in demo-output/pilot/stripe-fixture-later.json --store demo-output/pilot/real.db
uv run scruple ingest verify --store demo-output/pilot/real.db
```

```
wrote demo-output/pilot/stripe-fixture.json and demo-output/pilot/stripe-fixture-later.json
REAL Stripe API fixture objects (stripe/openapi, MIT), not customer data
stripe: 1 records read, 1 ok, 0 failed; 1 cases (1 new, 0 updated, 0 unchanged), 0 movements; 0 card numbers tokenised, 0 fields encrypted, 0 redactions; run 1
report written to demo-output/pilot/ingest-real.json
stripe: 1 records read, 1 ok, 0 failed; 1 cases (0 new, 1 updated, 0 unchanged), 0 movements; 0 card numbers tokenised, 0 fields encrypted, 0 redactions; run 2
case store demo-output/pilot/real.db
runs: 2 (0 synthetic), records read 2, failed 0 (0 errors listed)
cases: 1 (1 with later versions), movements: 0
aggregates (cells under k=10 suppressed):
  source: withheld (fewer than k=10 cases)
  state: withheld (fewer than k=10 cases)
...
raw card numbers in the file: 0
STORE OK
```

Say: the re-export after the dispute was lost is a new version, not an overwrite; the
store is append-only. One case is below k = 10, so every aggregate is withheld. The
payment facts read from the expanded charge (`ExternalCase.facts` in the store,
trimmed):

```
"card_brand": "visa", "card_funding": "credit", "card_country": "US", "cvc_check": "pass",
"avs_line1_check": null, "avs_postal_code_check": null,
"three_ds_result": null, "three_ds_authenticated": null,
"shipping_address_present": false, "shipping_tracking_present": false,
"refunded_before_dispute": false, "refund_state": "none", "mcc": null, "delivery_confirmed": null,
"enhanced_eligibility_types": ["visa_compelling_evidence_3"]
```

Say: the fixture's `three_d_secure` object has every field null, so 3-D Secure is
unknown, not false. MCC and delivery confirmation are not in Stripe's charge or dispute
schema, so they are null, not guessed. The billing name in the fixture is never read.

The SYNTHETIC history, through the same path:

```bash
uv run scruple backtest synth --n 3000 --seed 11 --out demo-output/pilot/SYNTHETIC-stripe-3000.json
uv run scruple ingest stripe --in demo-output/pilot/SYNTHETIC-stripe-3000.json --store demo-output/pilot/synthetic.db
uv run scruple ingest verify --store demo-output/pilot/synthetic.db
uv run scruple ingest stripe --in demo-output/pilot/stripe-fixture.json --store demo-output/pilot/synthetic.db; echo "exit $?"
```

```
wrote 3000 SYNTHETIC Stripe-shaped disputes to demo-output/pilot/SYNTHETIC-stripe-3000.json
stripe [SYNTHETIC DATA]: 3000 records read, 3000 ok, 0 failed; 3000 cases (3000 new, 0 updated, 0 unchanged), 2745 movements; 0 card numbers tokenised, 0 fields encrypted, 0 redactions; run 1
case store demo-output/pilot/synthetic.db
runs: 1 (1 synthetic), records read 3000, failed 0 (0 errors listed)
cases: 3000 (0 with later versions), movements: 2745
aggregates (cells under k=10 suppressed):
  source: stripe 3000
  state: closed 562, lost 1952, open 150, won 336
  stage: chargeback 121, inquiry 29, resolved 2850
  reason: fraudulent 776, general 747, product_not_received 772, product_unacceptable 705
  currency: USD 3000
...
raw card numbers in the file: 0
STORE OK
error: this store holds synthetic data; refusing to add real data to it
exit 2
```

Backtest a candidate (`docs/pilot/demo/fraud.json`: contest every fraud dispute) on it:

```bash
uv run scruple backtest run --history demo-output/pilot/synthetic.db --format store \
  --candidate docs/pilot/demo/fraud.json --out demo-output/pilot/backtest --no-pdf
sed -n '/## By reason/,/^Worse/p' demo-output/pilot/backtest/report.md
```

```
insufficient evidence [SYNTHETIC DATA]: 2288 cases (1987 observed, 301 estimated)
wrote demo-output/pilot/backtest/report.json
wrote demo-output/pilot/backtest/report.md
## By reason

| reason | n | observed | estimated | history | candidate observed | candidate estimated | mean diff [CI] | e regress | e improve | verdict | cases needed |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---:|
| fraudulent | 606 | 305 | 301 | 7,041,877 | 3,070,766 | 2,863,299 | -1,828 [-3,597, -61] | 1 | 66.5 | better | 260 |
| general | 566 | 566 | 0 | 6,726,265 | 6,726,265 | 0 | 0 [-1,511, 1,511] | 1 | 1 | no change | 9,447 (too few) |
...
```

Shadow mode on the same cases (week 4, replayed): each case is sent as production's
decision on the day it opened, with no outcome; then the outcomes arrive.

```bash
uv run python docs/pilot/demo/shadow_imported.py demo-output/pilot/synthetic.db docs/pilot/demo/fraud.json
```

```
[synthetic] SYNTHETIC: generated by scruple.calibration.synthetic from a...
recorded 3000 decisions: candidate agrees on 2529
before outcomes: 0 resolved, production cost None
outcomes arrived: 3000 updated, 2288 resolved
  observed  agreement                      0.84 (n=3000)
  observed  changed_decisions              471.00 (n=3000)
  observed  resolved_cases                 2,288.00 (n=3000)
  observed  production_cost                27,326,615.00 (n=2288)
  observed  candidate_cost_same_action     23,355,504.00 (n=1987)
  estimated candidate_cost_changed_action  2,863,299.01 [2,672,710 .. 3,037,668] (n=301)
  estimated candidate_minus_production     -1,107,811.99 [-1,298,401 .. -933,443] (n=2288)
```

Say: before outcomes only the observed counts have values; cost appears as cases
resolve, and the part that depends on a decision production did not take is labelled
estimated, with its band. It matches the backtest because the pricing is the same.
Nothing was written: shadow mode holds no storage handle. (No deadline or ledger
findings here: every decision was placed two hours after opening, and the synthetic
postings are clean.)

CI on their history, beside the synthetic gate:

```bash
uv run scruple ci run --config docs/pilot/demo/scruple.toml --candidate docs/pilot/demo/fraud.json \
  --out demo-output/pilot/fraud-history.json --history demo-output/pilot/synthetic.db; echo "exit $?"
uv run scruple ci render --scorecard demo-output/pilot/fraud-history.json | sed -n '/### Sources/,/^| `general`/p'
```

```
scruple gate: BLOCK fraud-contest-all@v1
  dec-00001 shadow  block          Blocked: 2 hard check(s) failed.
  REGRESSION check: no_slice_regression: worst slice 'fraud' mean regret change $78.57 per case (tolerance $0.00)
  REGRESSION check: not_significantly_worse: mean improvement -$23.26 per case, 95% CI [-$26.44, -$20.31]
  REGRESSION slice: fraud: mean regret worse by $78.57 per case over 888 cases
  source synthetic held-out cases: BLOCK
  source partner history [SYNTHETIC DATA]: HOLD (insufficient evidence; not enough resolved cases to decide either way) 2288 cases, 1987 observed, 301 estimated
    fraudulent               better                 n=606 (305 obs, 301 est)
    general                  no change              n=566 (566 obs, 0 est)
    product_not_received     no change              n=586 (586 obs, 0 est)
    product_unacceptable     no change              n=530 (530 obs, 0 est)
scorecard written to demo-output/pilot/fraud-history.json
exit 1
### Sources

| Source | Verdict | Cases |
|---|---|---|
| synthetic held-out cases | **BLOCK** | 3,000 |
| partner history (SYNTHETIC data) | **HOLD** | 2,288 (1,987 observed, 301 estimated) |

### Partner history: HOLD (SYNTHETIC data)
...
- Backtest verdict **insufficient evidence**: not enough resolved cases to decide either way.
- Cost, history (observed): 273,266.15 USD; candidate: 262,188.03 USD, of which observed 233,555.04 USD and estimated 28,632.99 USD (estimate band 260,282.14 USD to 263,931.72 USD)
- Mean change per case -4.84 USD, anytime-valid interval [-13.86 USD, 4.17 USD]
...
- Rule fields not in the export, default used (cases): currency_mismatch (2288), delivery_confirmed (2288), has_prior_refund (2288), mcc (2288), three_ds (2288)

| Reason | Cases | Observed | Estimated | Verdict | Cases needed |
|---|---:|---:|---:|---|---:|
| `fraudulent` | 606 | 305 | 301 | better | 260 |
| `general` | 566 | 566 | 0 | no change | - |
```

Say: the two sources disagree here, and the scorecard keeps them apart. The synthetic
simulator says contesting every fraud dispute is worse; this synthetic history says
better for fraud but not enough evidence overall. The gate takes the worse of the two.
In the pilot the second row is their history. The same command on the one real
fixture case:

```bash
uv run scruple ci run --config docs/pilot/demo/scruple.toml --candidate docs/pilot/demo/good.json \
  --out demo-output/pilot/good-real.json --history demo-output/pilot/real.db; echo "exit $?"
```

```
scruple gate: HOLD inr-delivered-contest@v1
  dec-00001 shadow  promote        Promoted to canary: improves outcomes with no regressions.
  dec-00002 canary  canary_passed  Canary passed: ready for human approval.
  source synthetic held-out cases: PASS
  source partner history: HOLD (insufficient evidence; fewer than k=10 cases: too few to calibrate or to decide) 1 cases, 1 observed, 0 estimated
    general                  no change              n=1 (1 obs, 0 est)
scorecard written to demo-output/pilot/good-real.json
exit 1
```

Say: one case is not evidence, and the gate says so instead of passing.

## 7. MCP and the Claude Code plugin (2 min)

The plugin in `plugins/claude-code` starts two MCP servers over stdio. This lists
their tools through a real MCP client:

```bash
uv run python docs/pilot/demo/mcp_tools.py
claude plugin validate plugins/claude-code
```

```
scruple-sim: 12 tools
  accept_liability, escalate, get_case, get_evidence, get_ledger_view, list_case_types, list_evidence, refund, represent, request_info, start_episode, submit_decision
scruple-gate: 6 tools
  evaluate_rule, get_evaluation_status, get_report, list_rules, submit_rule, verify_audit
...
√ Validation passed
```

Say: the gate server has no approve or rollback tool on purpose. The plugin's
commands are `/scruple-gate <rule.json>` and `/scruple-episode <seed>`. The manifests
validate; installing the plugin into a live Claude Code session was not part of this
check, and its `.mcp.json` assumes a checkout of this repository.

## Close (1 min)

- Everything shown except section 2 and the Stripe fixture in section 6 (Stripe's public
  API sample) is synthetic. No customer data has touched it.
- What the pilot adds: their history, their rules, their CI, and criteria they sign
  before we see data (README.md, SUCCESS_CRITERIA.md).
- What the pilot adds on top of section 6: their export instead of the fixture and the
  synthetic history, their own policy constraints as checks, and their candidate
  (SCOPE.md). What is not built is listed there and in SECURITY.md "Not yet".
