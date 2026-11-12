# Quickstart

Python 3.12 and [uv](https://docs.astral.sh/uv/). Everything below runs on synthetic
data.

## 1. Install and run the demo

```bash
git clone <repo> && cd agent-gate
make install                  # uv sync --python 3.12
make demo                     # the e-value gate on 30k synthetic cases, about 15 s
uv run scruple --help
```

The demo proposes a good rule and a bad rule, replays both in shadow, canaries the
good one, has a named approver activate it, rolls it back, and shows that tampering
with the audit log is detected.

## 2. Set up a tenant

```bash
uv run scruple init --yes --dir ./acme --tenant acme \
  --admin risk-admin@acme.example --key-out ./acme/admin.key
```

`scruple init` writes `scruple.toml` (no secrets), an approval policy skeleton
(`scruple-policy.toml`), migrates the database (PostgreSQL) or creates it (SQLite),
imports the synthetic baseline into the tenant, creates one `risk_admin` API key
(shown once, or written with mode 0600 to `--key-out`) and runs a smoke check. It is
safe to re-run: nothing is created twice and a key is never shown again. Without
`--yes` it asks for missing values and confirms the plan.

## 3. Serve the API and the console

```bash
export SCRUPLE_CONFIG=./acme/scruple.toml
uv run scruple serve                                   # http://127.0.0.1:8000
curl -s -H "X-API-Key: $(cat ./acme/admin.key)" localhost:8000/auth/whoami
```

The web console is mounted at `/console/` (or `uv run scruple console` on its own
port). The API and the console authenticate with API keys, OIDC bearer tokens, or
(console only) SAML sign-on.

## 4. Propose and evaluate a rule

```bash
uv run scruple tenant create acme      # optional: record the tenant explicitly
uv run scruple keys create --tenant acme --subject agent-1 --role proposer
uv run scruple propose rule.json        # the same model as POST /rules
uv run scruple evaluate <rule_id> 1     # shadow, then canary if it passes
uv run scruple approve <rule_id> 1 --approver <a named approver>
uv run scruple audit verify
```

## 5. See what was used

```bash
uv run scruple usage report             # gate runs, cases replayed, rollouts, per month
```

## Checks

```bash
make lint typecheck test                # what every commit passes
make docs                               # this site
```
