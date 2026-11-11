# Install

Four ways to run scruple. Python 3.12 only (`requires-python = ">=3.12,<3.13"`).
The distribution is `scruple-engine`; the import and the command are `scruple`.

Every command below was run on 2026-11-10 (Linux, Python 3.12, uv 0.11, Docker 29)
unless it says otherwise. Data in a fresh install is synthetic.

## 1. pip

**Not on PyPI yet.** Nothing is published until a licence is chosen (see
`LICENSE-PENDING.md`); the release workflow refuses to publish before then. Once a
release exists:

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install scruple-engine            # not yet verified: no release exists
scruple version
```

Until then, install from a checkout or a built wheel (both verified):

```bash
pip install /path/to/agent-gate                          # from source
uv build && pip install dist/scruple_engine-*.whl         # from a wheel
scruple version                                           # scruple 0.1.0 (commit unknown, ...)
scruple demo --help
python -c "import scruple; print(scruple.__version__)"
```

Extras (`pip install "scruple-engine[postgres,pdf]"`):

| extra | adds | note |
|---|---|---|
| `postgres` | psycopg | already in the core install today |
| `pdf` | fpdf2 | already in the core install today |
| `agents` | mcp, claude-agent-sdk, langgraph | mcp is core; the other two are not |
| `claude` | claude-agent-sdk | for `scruple.agents.adapters.claude_sdk` |
| `langgraph` | langgraph | for the LangGraph adapter |
| `dev` | pytest, hypothesis, ruff, mypy, ... | for running the tests from an sdist; uv users use the `dev` group |

The core extras exist so the install command keeps working if the core set is
slimmed later. Verified: `pip install --dry-run "dist/scruple_engine-0.1.0-py3-none-any.whl[postgres,pdf,agents]"` resolves.

Not in the wheel: `data/`, `results/`, the site and the docs. Commands that read
repository data (`scruple realdata`, `scruple bench`) need a checkout.

## 2. Docker

```bash
docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t scruple .
docker run --rm scruple version
docker run --rm -p 127.0.0.1:8000:8000 -v scruple-data:/data scruple   # API on :8000, SQLite in /data
```

The image runs as uid 10001, has no dev dependencies, and defaults to SQLite at
`/data/scruple.db`. Verified: the image builds and `version` prints the commit it was
built from. (In this sandbox the build needed a local mirror of the `uv` base image
and the proxy's CA bundle; a normal machine needs neither.)

## 3. Docker Compose (API plus PostgreSQL 16)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD; .env is untracked
docker compose up --build -d --wait
curl localhost:8000/version
curl -s "localhost:8000/rules?limit=1" -D - -o /dev/null | grep -i x-total-count
docker compose down           # add -v to delete the database volume
```

The entrypoint migrates Postgres to head before serving. Ports bind to 127.0.0.1
only; `POSTGRES_PORT` and `API_PORT` move them. Verified with both containers
reporting healthy and `/version` answering.

## 4. From source (development)

```bash
git clone <repo> && cd agent-gate
make install                  # uv sync --python 3.12 (runtime + dev group)
make demo                     # e-value gate on 30k synthetic cases
uv run scruple serve          # http://127.0.0.1:8000, SQLite at ./agent-gate.db
make lint typecheck test      # what every commit must pass
make test-slow                # slow suites, including the wheel install test
```

## Setting up a new partner: `scruple init`

One command, safe to re-run, with `--yes` for CI:

```bash
scruple init --yes --dir /etc/scruple --db /var/lib/scruple/scruple.db \
  --tenant acme --admin risk-admin@acme.example --key-out /run/secrets/scruple-admin.key
SCRUPLE_CONFIG=/etc/scruple/scruple.toml scruple serve
```

It writes `scruple.toml` (no secrets: a password in `--db` is used for this run and
never written; set `SCRUPLE_DATABASE_PASSWORD`), writes `scruple-policy.toml` (an
approval policy skeleton, filled from `--approver name=role` and `--required`),
migrates Postgres to the alembic head, imports the synthetic baseline into the
tenant, creates one `risk_admin` key (shown once, or written 0600 to `--key-out`)
and runs an in-process smoke check (audit chain, `/health`, anonymous refusal, the
new key). A re-run validates what exists, keeps it, creates nothing twice and never
shows a key again; flags that disagree with an existing config are refused. Without
`--yes` it asks for missing values and confirms the plan; with no terminal and no
`--yes` it refuses. Verified by `tests/test_cli_init.py`, on SQLite and Postgres 16.

## Clients

- Python: `from scruple import Client` (in the main package; httpx). Typed with the
  server's own models, retries idempotent calls, pages with `iter_rules()` and
  `iter_decisions()`.
- TypeScript: `sdk/typescript` (types generated from the OpenAPI schema; not
  published). `cd sdk/typescript && npm ci && npm test`. Verified, including the
  opt-in run against a live server and the MCP simulator
  (`SCRUPLE_SDK_INTEGRATION=1 npm test`).
- MCP: `scruple mcp sim` (dispute simulator) and `scruple mcp gate`, on stdio.

## Releasing

See `deploy/github-workflows/publish.yml`: bump `project.version`, tag `vX.Y.Z`, push
the tag. The workflow checks the tag matches the version, runs the checks, builds,
runs `twine check`, publishes to PyPI by trusted publishing (no token), and attaches
the wheel, sdist and checksums to the GitHub release with notes from
`scripts/changelog.py`. `CHANGELOG.md` is regenerated with
`uv run python scripts/changelog.py`.
