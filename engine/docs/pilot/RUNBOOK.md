# Pilot runbook: day 0 to readout, both sides

Who does what, in what order, with the exact commands. SCOPE.md says what the pilot is;
this file says how to run it. **P** is the partner, **S** is scruple. Nothing here has
been run with a partner yet.

How the commands were checked: every command not marked otherwise was run on
2026-11-11 against this tree on a synthetic history (`scruple backtest synth`), in the
order below, and did what the line says. The same day, `scruple pilot run` and
`scruple pilot readout` ran on a synthetic history, `scruple bench seal` sealed a small
synthetic set, and `scruple ingest stripe-pull` was checked for its flags and key
handling only (it needs a Stripe test-mode key and network;
tests/test_ingest_stripe_pull.py covers it on Stripe's published fixtures). Run everything from the engine directory
(`uv run scruple ...`), or as `docker run IMAGE ...` with the image.

## Where things live

| thing | where | who holds it |
|---|---|---|
| case store (`cases.db`) | partner's environment (default) or the scruple machine, tokenised and redacted | whoever runs ingest; never both synthetic and real in one store |
| gate database (`pilot.db` or a Postgres schema) | same place as the case store | S operates, P can read with an `auditor` key |
| field and token keys (`SCRUPLE_FIELD_KEYS`, `SCRUPLE_TOKEN_KEYS`) | P's secret manager, environment only | P; deleting them crypto-shreds |
| held-out salt (`SCRUPLE_HELDOUT_SALT`) | environment only | S; per-partner salt and handover: `scruple bench seal --partner <code> --out handover/<code>` (salt in `~/.scruple/partners/`, mode 0600) |
| audit anchors | a git repository P controls, or RFC 3161 tokens | P (so S cannot rewrite history unseen) |
| readout, scorecards, evidence packs | as TERMS_DRAFT.md allows | both |

## Day 0: before kickoff

| step | side | action |
|---|---|---|
| 0.1 | both | NDA and pilot terms signed (TERMS_DRAFT.md, after counsel). |
| 0.2 | P | Planner inputs: monthly volume, reason-code mix, smallest improvement worth detecting, cost cap, outcome lag. |
| 0.3 | S | Size it with the planner and send the output with the proposal. |
| 0.4 | both | Deployment choice: partner environment (image or package) or scruple machine (SECURITY.md section 1). |
| 0.5 | P | Security review: docs/trust/QUESTIONNAIRE.md, docs/pilot/SECURITY.md. |

```bash
# 0.3 planner (illustrative inputs; use the partner's)
uv run python scripts/pilot_plan.py --monthly-volume 3000 \
  --mix fraudulent=0.5,product_not_received=0.5 \
  --min-improvement 2 --cost-cap 200 --outcome-lag-weeks 8
```

With those illustrative inputs the planner says the cost hypothesis needs about 137
months of history: a pilot of that size cannot test a $2 per case improvement, and the
criteria should say so (SUCCESS_CRITERIA.md). Say it before the pilot, not after.

### Environment (P runs, S assists)

```bash
docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t scruple .   # not rebuilt for this runbook (no Docker daemon here)
docker run --rm scruple version          # or: uv sync && uv run scruple version (checked)
uv run scruple realdata verify           # the committed CFPB splits match their hashes
```

### Keys, one per person or system, least privilege (P creates, as local admin)

```bash
export SCRUPLE_DATABASE_URL=pilot.db     # or postgresql://scruple@host/scruple (+ SCRUPLE_DATABASE_PASSWORD)
uv run scruple keys create --subject dispute-agent-01 --role proposer --expires-days 60
uv run scruple keys create --subject prod-shadow-feed --role service --expires-days 60
uv run scruple keys create --subject approver@partner.example --role approver --expires-days 60
uv run scruple keys create --subject reviewer@partner.example --role auditor --expires-days 60
uv run scruple keys list
```

Each key prints once; store it in P's secret manager. One key may not hold both
`propose` and `approve` (refused). The approver must not be the rule's owner or its
learning agent (`scruple/gate/approval.py`). Subjects above are placeholders.

### Audit anchor repository (P owns it)

```bash
git init audit-anchors && git -C audit-anchors config user.name "<P's ops name>"
git -C audit-anchors config user.email "<P's ops email>"
# add a remote P controls with protected history; scruple never pushes
```

### Dry run on synthetic data (S, before any partner data)

```bash
mkdir -p dry && cd dry
uv run scruple backtest synth --n 2000 --seed 7 --out history.json      # SYNTHETIC
uv run scruple ingest stripe --in history.json --store synthetic.db \
  --report ingest.json --label synthetic-dry-run
uv run scruple ingest verify --store synthetic.db                       # ends "STORE OK"
uv run scruple backtest run --history synthetic.db --format store \
  --candidate ../docs/pilot/demo/good.json --out backtest
cp ../docs/pilot/demo/scruple.toml ../docs/pilot/demo/bad.json .
uv run scruple ci run --config scruple.toml --history synthetic.db --evidence evidence
echo $?                                  # 1: the planted bad candidate is blocked
cd .. && rm -rf dry
```

## Week 1: kickoff

| step | side | action |
|---|---|---|
| 1.1 | both | Sign SUCCESS_CRITERIA.md. **Nothing is loaded before this is signed.** Commit the signed file's sha256 to the anchor repo too. |
| 1.2 | P | Walk through the current rules or agent version and the candidate change. |
| 1.3 | S | Write the current rules and the candidate as `RuleSpec` JSON (shape: `docs/pilot/demo/good.json`), or a Python `decide(view)` module for the backtest. |
| 1.4 | S | Write P's own policy constraints as `extra_checks`, with tests, before any backtest. |
| 1.5 | P | Agree the export (DATA_CHECKLIST.md): Stripe API pull, Stripe or Adyen export file, or CSV with an `ImportConfig`. |
| 1.6 | S | For CSV: write `import.json` (ImportConfig) and test it with `--dry-run` on P's sample. |

```bash
sha256sum SUCCESS_CRITERIA.signed.pdf > audit-anchors/criteria.sha256
git -C audit-anchors add criteria.sha256 && git -C audit-anchors commit -m "signed criteria"
```

## Week 2: export and ingest (P runs in P's environment)

```bash
export SCRUPLE_TOKEN_KEYS="k1:<base64url 32 bytes>"     # from P's secret manager
export SCRUPLE_FIELD_KEYS="k1:<base64url 32 bytes>"

# Stripe, from the API in test mode (paged, resumable); STRIPE_API_KEY from the environment
uv run scruple ingest stripe-pull --since 2026-01-01 --until 2026-10-01 \
  --out export.jsonl --store cases.db --report ingest.json

# or from an export file (exists)
uv run scruple ingest stripe --in disputes.json --store cases.db --report ingest.json --label partner-2026
uv run scruple ingest adyen  --in notifications.json --store cases.db --report ingest.json   # SCRUPLE_ADYEN_HMAC_KEYS
uv run scruple ingest csv    --in cases.csv --store cases.db --config import.json \
  --pan-column <card column> --encrypt-column <column> --report ingest.json

uv run scruple ingest verify --store cases.db          # must end "STORE OK"; "raw card numbers in the file: 0"
```

- Exit 0: all rows stored. Exit 1: some rows failed, listed in `ingest.json` with row,
  column and message. Exit 2: the file or options were rejected. `--strict` stores
  nothing if any row fails; `--dry-run` writes nothing.
- Exit criterion (SCOPE.md): more than the agreed share of rows rejected for reasons a
  mapping change cannot fix ends the pilot early.
- P's owner signs off the `ingest verify` output (it suppresses cells under 10).

## Week 3: backtest

```bash
uv run scruple backtest run --history cases.db --format store \
  --candidate candidate.json --out backtest --label partner-2026
# planted regressions: copies of the candidate with a known defect (template: docs/pilot/demo/bad.json)
uv run scruple backtest run --history cases.db --format store \
  --candidate planted-1.json --out backtest-planted-1
```

Report: `backtest/report.{json,md,pdf}`, per reason code: decisions changed (exact),
cost on unchanged decisions (observed), cost on changed decisions (estimated, with an
optimistic and a pessimistic end). Estimated is never reported as measured.

## Week 4: shadow on live traffic

P's production keeps deciding; scruple scores and acts on nothing.

```bash
# the gate as a service, behind P's TLS proxy (docs/DEPLOY.md)
uv run scruple serve --host 127.0.0.1 --port 8000
# P's feed, with the `service` key:
curl -H "Authorization: Bearer $SERVICE_KEY" -H 'content-type: application/json' \
  --data @decisions.json \
  https://<gate>/shadow/rules/<rule_id>/versions/<v>/imported/decisions
curl -H "Authorization: Bearer $SERVICE_KEY" -H 'content-type: application/json' \
  --data @outcomes.json \
  https://<gate>/shadow/rules/<rule_id>/versions/<v>/imported/outcomes
curl -H "Authorization: Bearer $AUDITOR_KEY" https://<gate>/shadow/rules/<rule_id>/versions/<v>/imported
```

The same from the command line: `scruple shadow imported record|observe|summary|history`
(`record RULE_ID VERSION FILE --actor ...`). Replay from a store, as a rehearsal:
`uv run python docs/pilot/demo/shadow_imported.py cases.db candidate.json`.

Outcomes lag. Shadowed decisions mostly resolve after week 6; the readout says which
cost results are still open.

## Week 5: gate in P's CI

| step | side | action |
|---|---|---|
| 5.1 | P | Add the GitHub Action (`.github/actions/scruple-gate`) or GitLab template (`.gitlab/scruple-gate.yml`) to one repository (docs/CI.md). |
| 5.2 | P | Make `scruple/gate` a required status check. |
| 5.3 | P | Put the case store where the runner can read it, or point `--history` at the export. |

```bash
uv run scruple ci run --config scruple.toml --history cases.db --history-label partner-2026 \
  --db pilot.db --evidence evidence
uv run scruple ci render --scorecard out/scorecard.json
uv run scruple ci evidence --db pilot.db --out evidence <rule_id> <version> --scorecard out/scorecard.json
```

Exit 1 means the gate blocked. A history verdict of "insufficient evidence" is a HOLD.

## Week 6: readout and deletion

One command for ingest, calibration, backtest, shadow, gate and evidence pack, and one
to fill the readout from it (docs/pilot/README.md):

```bash
uv run scruple pilot run --history cases.db --candidate rule.json --out pilot/   # the ingested store
uv run scruple pilot readout --run pilot/ --criteria criteria.json --out readout/
```

```bash
uv run scruple audit verify --db pilot.db                       # "INTEGRITY OK"
uv run scruple audit anchor --db pilot.db --git-repo audit-anchors
git -C audit-anchors push                                        # P pushes
uv run scruple audit verify-anchor --db pilot.db --git-repo audit-anchors --commit <sha>
# or an RFC 3161 timestamp from a TSA P chooses:
uv run scruple audit anchor --db pilot.db --tsa-url <tsa url> --tsa-cert tsa.pem --out anchor.json
uv run scruple audit verify-anchor anchor.json --db pilot.db --tsa-cert tsa.pem
```

| step | side | action |
|---|---|---|
| 6.1 | S | Fill READOUT_TEMPLATE.md: each pre-registered criterion is pass, fail or not enough data. |
| 6.2 | both | Review with P's reviewer (auditor key, evidence packs). |
| 6.3 | S | Final anchor of the gate audit head (above); P pushes it. |
| 6.4 | both | Deletion (below), then the written confirmation. |

### Deletion

```bash
uv run scruple keys revoke --db pilot.db <key_id>      # every pilot key; takes effect on the next request
uv run scruple retention set --db pilot.db --days 1 --actor <S operator>   # optional, audited purge first (1 is the minimum)
uv run scruple retention apply --db pilot.db --case-store cases.db --actor <S operator>
rm cases.db pilot.db ingest.json export.jsonl* && rm -r backtest* evidence out
# P: delete SCRUPLE_FIELD_KEYS and SCRUPLE_TOKEN_KEYS from the secret manager (crypto-shredding)
```

The audit log and decisions are append-only; deleting pilot data means deleting the
whole pilot database and case store, not rows. Keep the anchor repository and the
readout only as the terms allow. `retention apply` records each purge in the audit log
(rows, cutoff, digest) before the files go; it is optional when the files are deleted
anyway.

## If something goes wrong

- Audit chain broken, or `verify-anchor` fails: stop writes, snapshot, follow
  docs/DEPLOY.md "Incident response" and docs/trust/INCIDENT_RESPONSE.md. Tell P the
  same day.
- A card number or free text found where it should not be: stop ingest, keep the
  report, delete the store, fix the mapping, re-run. Tell P the same day.
- A key leaked: `scruple keys revoke`, then create a new one.
