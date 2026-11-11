# Change management policy

Owner: the founder. Review: every six months. Not legal advice; see README.md for the
status words.

Two kinds of change: changes to scruple itself (code, images, infrastructure), and
changes to a partner's learned rules, which scruple exists to control.

## Changes to scruple

| control | how | status |
|---|---|---|
| small commits, one idea each, keyed messages | `[+]`, `[!]`, `[~]`, `[#]` (CLAUDE.md); the changelog is generated from them (`scripts/changelog.py`) | process; changelog generation is code |
| every commit passes lint, types and tests | `make lint typecheck test`; CI repeats it on push and pull request (`.github/workflows/ci.yml`: lint, typecheck, tests with coverage, Postgres with migrations, compose check, image build; a weekly slow suite) | code (CI), process (locally) |
| peer review before merge | one person: no second reviewer exists | not yet |
| branch protection on `main` | | not verified; set it when a second person joins |
| deterministic outputs | seeds and an injected clock; the demo output is tested byte-identical across runs (tests/test_cli.py) | code |
| known weaknesses pinned | tests/test_known_gaps.py fails on purpose when a gap is fixed, so docs are updated with the fix | code |
| dependency changes | locked in `uv.lock`; CI installs with `uv sync --locked` | code |
| images | base images pinned by digest (tests/test_deploy_server.py fails on a placeholder); release builds, scans with Trivy (fixable HIGH/CRITICAL fail before push), pushes with an SBOM and provenance, signs with cosign keyless (deploy/github-workflows/release.yml) | code; the workflow has never run on GitHub (docs/DEPLOY.md) |
| deploy by digest, staging first | the same digest that passed staging goes to prod; Terraform refuses an image without a digest; smoke test after each deploy (`scripts/smoke.sh`) | code (refusal, smoke script), process (promotion) |
| database migrations | Alembic, one head; expand, deploy, contract; never downgrade a database with real history (docs/DEPLOY.md "Migrations"); `/ready` refuses traffic on an older schema | code (head and readiness checks), process (expand/contract) |
| rollback | `helm rollback`, ECS circuit breaker, then `scruple audit verify` (docs/DEPLOY.md "Rollback") | process; never exercised on a cluster |
| security scan of the running app | ZAP baseline on pull requests and weekly (deploy/github-workflows/zap.yml) | code; never run on GitHub |
| emergency change | allowed without waiting for CI only to stop harm (revoke a key, scale to zero); the commit and an incident note follow the same day | process |

## Changes to a partner's learned rules (the product)

| control | how | status |
|---|---|---|
| no rule goes live unverified | candidate, shadow replay plus verifiers, canary, named human approval; the state machine is hardcoded (`scruple/registry/`), and `AgentGate` is the only place verdicts change state (`scruple/service.py`) | code |
| every transition recorded | hash-chained, append-only audit log with tenant genesis (`scruple/audit/log.py`); anchors outside the database (`scruple audit anchor`, `scruple/audit/anchor.py`) | code; anchoring on a schedule is process |
| evidence per decision | `scruple ci evidence` (JSON and PDF); report per decision (`scruple report`) | code |
| CI gate on the partner's repository | GitHub Action and GitLab template block a regressing candidate (docs/CI.md) | code; not yet run on github.com or gitlab.com |
| undo in one step | `POST /rules/{id}/rollback`, audited; automatic rollback when assurance fails | code |
