# Live demo script (20 minutes)

Every command below was run in this repository on 2026-11-09 and the output pasted,
trimmed where marked `...`. Run them from the `engine/` directory. Outputs go to
`demo-output/pilot/`, which git ignores. Everything except section 2 is synthetic
simulator data; say so out loud each time.

Hashes that come from a file database (`--db demo-output/pilot/gate.db`) include wall
clock time and differ on every run. Scorecards from `scruple ci run` use an in-memory
database and a fixed clock, so they are byte-identical across runs.

## Before the meeting (10 minutes, not shown)

Needs `uv`, `jq`, `pdftotext` (poppler) and, for section 6, the `claude` CLI.

```bash
make install
make lint typecheck test         # about 2,350 tests, about 2.5 minutes
rm -rf demo-output/pilot && mkdir -p demo-output/pilot
uv run scruple realdata verify   # checks the committed CFPB splits against their hashes
```

Do not run `scripts/compare_real_models.py` live: it refits the baselines and took
5.5 minutes here. Its output is committed in `results/real/models/SUMMARY.md`.

## Running order

| minute | section | point to make |
|---|---|---|
| 0-4 | 1. the claim and the ledger | an agent's self-report is not evidence |
| 4-7 | 2. real data | the gap is real: models know rules, misjudge outcomes |
| 7-11 | 3. a pull request gets blocked | the check lives in their release path |
| 11-14 | 4. canary and rollback | nothing goes live without a named human; undo is one step |
| 14-16 | 5. evidence pack | what their reviewer reads |
| 16-18 | 6. MCP and the Claude Code plugin | how an agent team plugs in |
| 18-20 | close | what is synthetic, what the pilot measures on their data |

## 1. The claim and the ledger (4 min)

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

## 2. Real data (3 min)

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

## 3. A pull request gets blocked (4 min)

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

Say: today this gate replays on synthetic held-out cases against a synthetic baseline.
In the pilot it is pointed at their history (SCOPE.md week 3 and 5). The action has
not yet run on github.com itself; it is tested locally and through a fake HTTP
transport.

## 4. Canary and rollback (3 min)

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

(Generate the evidence pack in section 5 now, while the rule is active, then roll back.)

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

Run right after the approval in section 4:

```bash
uv run scruple ci evidence --db demo-output/pilot/gate.db --out demo-output/pilot/evidence inr-delivered-contest 1
pdftotext demo-output/pilot/evidence/evidence-inr-delivered-contest-v1.pdf - | head -40
```

```
evidence pack f2055b17f9175bde written to demo-output/pilot/evidence/evidence-inr-delivered-contest-v1.json and .pdf
Evidence pack: inr-delivered-contest@v1
Verdict: PASS

Status: active
...
Seed commitments: canary 0fcd7ee7e7b4385b, eval 7a2658cf1a1a23f8
Data: synthetic simulator data only. Thresholds are illustrative defaults.
...
Decisions
dec-00001 shadow promote by agent-gate: Promoted to canary: improves outcomes with no regressions.
dec-00002 canary canary_passed by agent-gate: Canary passed: ready for human approval.
dec-00003 activation activated by model-risk@demo.bank: Activated by model-risk@demo.bank.

Regressions
None found.

Statistics
Mean regret improvement $11.58 per case, 95% interval [$9.08, $14.13], 3000 held-out cases, method percentile-bootstrap-paired.
Canary dec-00002 at 10%: canary_passed, treatment $17.47 vs control $118.27 (60/625 matched)
...
Audit chain proof
Segment seq 7 to 13 of 13 entries
...
Proof: OK
```

Open the PDF itself on screen. Point at: the seed is a commitment, not the seed; the
audit segment recomputes to the chain head; the "Data: synthetic" line.

Note: a pack exported after a rollback shows `Verdict: BLOCK` with `Status: retired`,
because the verdict is read from the last decision. Export before rolling back for
this demo.

## 6. MCP and the Claude Code plugin (2 min)

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

## Close (2 min)

- Everything shown except section 2 is synthetic. No customer data has touched it.
- What the pilot adds: their history, their rules, their CI, and criteria they sign
  before we see data (README.md, SUCCESS_CRITERIA.md).
- What we build in the pilot: the ingest command, the backtest on imported cases and
  the shadow adapter (SCOPE.md).
