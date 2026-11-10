# Deploying the scruple engine

How the engine gets from a commit to a running, observable, recoverable service.
All data the engine produces is synthetic unless you import your own cases.

**What has and has not been exercised.** See "Validation status" at the end. In
short: the image, gunicorn, health, metrics, tracing, smoke script and the
backup/restore scripts were run here; the Helm chart was linted, rendered and
schema-checked but never installed on a cluster; the Terraform module passes
`terraform validate` and has **never been planned or applied against a real AWS
account**; the release workflow passes actionlint and has never run on GitHub.

## Files

| path | what |
|---|---|
| `Dockerfile`, `docker/entrypoint.sh`, `deploy/docker/gunicorn.conf.py` | production image |
| `docker-compose.yml` | local: API plus PostgreSQL 16 |
| `deploy/github-workflows/release.yml` | build, scan, push, sign the image |
| `deploy/helm/scruple/` | Kubernetes chart |
| `deploy/terraform/aws/` | single-tenant AWS skeleton (ECS Fargate, ALB, RDS, Secrets Manager), UNTESTED |
| `deploy/scripts/backup.sh`, `restore.sh` | logical backup and verified restore |
| `scripts/smoke.sh` | post-deploy smoke test against any base URL |
| `scruple/api/health.py` | `/health`, `/ready` |
| `scruple/observability/` | `/metrics`, request and gate metrics, optional tracing |
| docs/SLOs.md | proposed SLOs and alerts |

## GitHub workflows in the monorepo

The engine lives under `engine/` in the `thedhruvhegde/scruple` repository. GitHub
only runs workflows from the repository root, so workflow files are kept here
under `deploy/github-workflows/` and copied to the root when the engine is synced:

```
engine/deploy/github-workflows/release.yml  ->  .github/workflows/engine-release.yml
```

Each such workflow sets `defaults.run.working-directory: engine`, passes
`engine` as the Docker build context, and filters on `paths: [engine/**]` so site
changes do not build images. Release tags are `engine-vX.Y.Z` (the site and the
engine share one tag namespace). Edit the copy here, then sync; never edit only the
root copy.

## The image

```
docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t scruple .
docker run --read-only --tmpfs /tmp -v scruple-data:/data -p 8000:8000 scruple
```

- Non-root (uid/gid 10001, numeric so Kubernetes `runAsNonRoot` can check it).
- Works with a read-only root filesystem: bytecode is compiled at build time, the
  only writable paths are `/tmp` (mount tmpfs/emptyDir) and `/data` (SQLite only).
- PID 1 is tini (static binary from the v0.19.0 release, sha256-checked per
  architecture at build time). It forwards SIGTERM to gunicorn, which stops
  accepting, lets workers finish for `SCRUPLE_WEB_GRACEFUL_TIMEOUT` seconds, exits 0.
- `CMD` is gunicorn with uvicorn workers; any other command runs the CLI
  (`docker run IMAGE version`, `docker run IMAGE audit verify`).
- The entrypoint migrates a Postgres database to head first, unless
  `SCRUPLE_MIGRATE_ON_START=false` (Helm and ECS set it; they migrate in a Job/task).
- `HEALTHCHECK` calls `/health`.

### Base image digests

Both base images are pinned by multi-arch index digest in `ARG PYTHON_IMAGE` and
`ARG UV_IMAGE`. The digests in the Dockerfile were read on 2026-11-10 with
`docker buildx imagetools inspect`. To refresh (monthly, or when the scan flags a
fixed base CVE):

1. `docker buildx imagetools inspect python:3.12-slim | grep Digest`
   and the same for `ghcr.io/astral-sh/uv:<version>`.
2. Replace the `@sha256:...` part of the ARG (keep the tag for readers).
3. Build, run `make test` against the image (`docker run IMAGE version`), push a
   branch; the release workflow's Trivy step must pass before it can be tagged.

A digest is never a placeholder in a release: `tests/test_deploy_server.py` fails
unless both ARGs carry a 64-hex digest.

### Server settings

All from `scruple.settings` (env `SCRUPLE_*` or the `SCRUPLE_CONFIG` TOML file):

| setting | default | notes |
|---|---|---|
| `SCRUPLE_API_HOST` / `_PORT` | image: `0.0.0.0` / `8000` | |
| `SCRUPLE_WEB_WORKERS` | 1 | 1..64. Shadow recorders keep state in worker memory: keep 1 if you use `/shadow/...` |
| `SCRUPLE_WEB_TIMEOUT` | 60 | seconds before a silent worker is killed; evaluation runs inside a request, so raise it with `GateConfig.n_cases` |
| `SCRUPLE_WEB_GRACEFUL_TIMEOUT` | 30 | keep below the orchestrator's kill timeout (Helm: 25 vs 40) |
| `SCRUPLE_WEB_KEEPALIVE` | 5 | set above the load balancer idle timeout (ALB 60, so 75) |
| `SCRUPLE_WEB_MAX_REQUESTS` | 0 | recycle workers after N requests (jitter N/10) |
| `SCRUPLE_READY_AUDIT_TTL_SECONDS` | 30 | cache for the readiness audit-chain check |
| `SCRUPLE_METRICS_TOKEN` | none | env only (secret). Bearer token for `/metrics` |
| `SCRUPLE_METRICS_PUBLIC` | false | serve `/metrics` without a token |
| `SCRUPLE_OTEL_ENABLED` | false | tracing (needs the `otel` extra; the image has it) |
| `SCRUPLE_OTEL_SERVICE_NAME` | scruple-engine | |
| `SCRUPLE_DATABASE_URL` | image: `/data/scruple.db` | postgres URL without a password in every shared environment |
| `SCRUPLE_DATABASE_PASSWORD`, `SCRUPLE_HELDOUT_SALT` | none | env only (secrets) |

Secrets are refused from the TOML file and never appear in the chart, compose
file, Terraform variables or image.

## Environments

| | local | staging | prod |
|---|---|---|---|
| how | `make serve` (SQLite) or `docker compose up --build` (Postgres 16) | Helm chart or the Terraform skeleton | same as staging, separate account/cluster |
| database | SQLite file, or the compose Postgres | managed Postgres, own instance | managed Postgres, Multi-AZ, deletion protection |
| image | built locally | release digest | the SAME digest that passed staging |
| data | synthetic | synthetic plus any partner test exports | only what the pilot agreement allows |
| smoke | `scripts/smoke.sh http://127.0.0.1:8000` | smoke with `SCRUPLE_SMOKE_API_KEY` (writes) | read-only smoke (no key) |
| metrics | public is fine | token plus NetworkPolicy | token plus NetworkPolicy; ALB never serves `/metrics` |

Local Postgres: `POSTGRES_PASSWORD=... docker compose up --build`, or `make db-up`
and `make db-migrate` with `make serve`.

## AWS (Terraform skeleton, UNTESTED)

`deploy/terraform/aws` describes one tenant: an ECS Fargate service (arm64) behind
an ALB (HTTPS only, `/metrics` answered with 404 at the edge, target health on
`/ready`), RDS PostgreSQL 16 (private, encrypted, `rds.force_ssl`, Multi-AZ, 14
days of backups, deletion protection in prod), Secrets Manager for the metrics
token and held-out salt, and an RDS-managed master password that never enters
Terraform state. It expects an existing VPC and ACM certificate.

It passes `terraform validate` (Terraform 1.9.8, hashicorp/aws 5.80.0). It has
never been planned or applied against an AWS account. Before first use:

1. `terraform init`, then `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64`
   and commit `.terraform.lock.hcl`.
2. Add a remote state backend (S3 with locking); none is configured.
3. `terraform plan -var-file=staging.tfvars` in a sandbox account and read every
   resource. `example.tfvars` shows the inputs; every id in it is a placeholder.
4. Put the secret values in out of band (`aws secretsmanager put-secret-value`).
5. The service connects as the RDS master user. Create a separate application
   role and grant it only what it needs (`scruple.storage.postgres.app_role_grants`)
   before holding real evidence, so the service cannot drop the append-only triggers.

## Release and promotion

1. Merge to `main`. The release workflow builds `edge` and `sha-<sha>` images.
2. Tag: `git tag engine-v0.2.0 && git push origin engine-v0.2.0` (owner only). The
   workflow builds linux/amd64 and linux/arm64, scans (fixable HIGH/CRITICAL fail
   the run before anything is pushed), pushes with an SBOM and max provenance,
   signs the digest with cosign keyless and prints the digest in the run summary.
3. Verify before deploying anywhere:
   ```
   cosign verify ghcr.io/thedhruvhegde/scruple-engine@sha256:<digest> \
     --certificate-identity-regexp '^https://github.com/thedhruvhegde/scruple/' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
   ```
4. Staging: `helm upgrade --install scruple deploy/helm/scruple -n scruple-staging -f staging.yaml --set image.digest=sha256:<digest> --wait`.
   The pre-upgrade Job migrates first; pods roll one at a time (maxUnavailable 0).
5. `SCRUPLE_SMOKE_API_KEY=... SCRUPLE_SMOKE_EXPECT_COMMIT=<sha> scripts/smoke.sh https://<staging>`.
6. Watch error rate and latency against docs/SLOs.md for at least a day.
7. Prod: the same command with the same digest and the prod values file; then
   `scripts/smoke.sh https://<prod>` without a key (read-only).

Never deploy by tag; tags move, digests do not. The Terraform module refuses an
image without a digest.

### Smoke test

`scripts/smoke.sh BASE_URL` checks `/health`, `/ready` (every check named),
`/version` and that `/metrics` is closed to anonymous callers. With
`SCRUPLE_SMOKE_API_KEY` it also registers a throwaway rule `smoke-<unix time>`
(escalate, matches nothing real) and evaluates it, which writes to the append-only
audit log: staging only. `SCRUPLE_SMOKE_EXPECT_COMMIT` fails it unless `/version`
reports that commit. The key is a scruple API key with the `proposer` role, made in
the staging database with `scruple keys create --subject smoke-test --role proposer`
and sent as `Authorization: Bearer <key>`. The script asks `/auth/whoami` who the key
is and registers the rule with that subject as owner and provenance agent; no
`X-Actor` header is sent (the server ignores it outside the development-only mode).
A bad key fails at `whoami` before anything is written; a key without `propose`
fails at `register` (403).

## Migrations

- Alembic, under `scruple/storage/migrations/`, one head (tested).
- Kubernetes: the chart's Job (`helm.sh/hook: pre-install,pre-upgrade`) runs
  `python -m scruple.storage.migrate` (alembic upgrade head) and then imports the
  synthetic baseline once, so replicas never race on either. A failed Job fails
  the release and the old pods keep running.
- ECS: run the `<prefix>-migrate` task definition with `aws ecs run-task` (command
  in `deploy/terraform/aws/ecs.tf`) and wait for exit code 0 before updating the
  service.
- Compose / single container: the entrypoint migrates on start.
- Rules for writing migrations: expand, deploy, contract. A migration must keep
  the previous release working (add columns nullable, backfill, then enforce in a
  later release), because rollback does not run migrations backwards.
- Never `alembic downgrade` a database with real history: audit events, decisions
  and rule content are append-only and a downgrade that drops a column destroys
  evidence. Restore from backup instead.
- `/ready` reports not ready until `alembic_version` equals the code's head, so a
  pod with newer code never takes traffic on an older schema.

## Rollback

This is rolling back a deploy. Rolling back a learned rule is an application
action (`POST /rules/{id}/rollback`, audited) and has nothing to do with this.

- Helm: `helm history scruple -n <ns>` then `helm rollback scruple <revision> --wait`.
  The migration hook does not run on rollback; the previous code runs on the newer
  schema, which the expand/contract rule above guarantees.
- ECS: the deployment circuit breaker rolls back a deploy whose tasks never pass
  health checks. Manually: `aws ecs update-service --cluster <c> --service api
  --task-definition <family>:<previous revision>`.
- After any rollback: smoke test, then `scruple audit verify` against the
  database (from a pod: `kubectl exec deploy/scruple -- scruple audit verify`).

## Backups and restore

Two layers:

1. Managed: RDS automated backups with point-in-time recovery
   (`db_backup_retention_days`, default 14) and a final snapshot on prod deletion.
2. Logical, portable, verified: `deploy/scripts/backup.sh` and `restore.sh`.

```
PGPASSWORD=... deploy/scripts/backup.sh postgresql://scruple@db:5432/scruple /backups/scruple-$(date -u +%Y%m%dT%H%M%SZ)
PGPASSWORD=... deploy/scripts/restore.sh /backups/scruple-20261110T030000Z postgresql://scruple@db:5432/scruple_restore
```

`backup.sh` writes `<prefix>.dump` (pg_dump custom format), `.sha256` and `.head`
(the audit chain head at backup time). Keep `.head` somewhere the database
operator cannot edit (it is the external anchor, docs/ARCHITECTURE.md section 6).
`restore.sh` only restores into an empty database, then checks the checksum, the
migration head, `scruple audit verify` (chain plus registry and decision
projections) and that the recorded head entry is present and unchanged.

Tested: `tests/test_deploy_backup.py` runs both scripts against the throwaway
PostgreSQL cluster the storage tests use (populated by a real gate run), and
checks that a corrupted dump, a non-empty target and a wrong head are refused.
It is marked `postgres` and runs in `make test` when the binaries are installed.

Restore drill (quarterly, docs/SLOs.md SLI 7): restore last night's dump into a
scratch database, run `restore.sh`, record the result and the time it took.

## Key rotation

| secret | where | rotate how | impact |
|---|---|---|---|
| database password | RDS-managed secret (Terraform) or your Secret | `aws secretsmanager rotate-secret`, or update the Kubernetes Secret; then `kubectl rollout restart deploy/scruple` (ECS: `update-service --force-new-deployment`) | each worker holds one connection opened at start: running pods keep working until restarted; new pods need the new password |
| metrics token | `SCRUPLE_METRICS_TOKEN` Secret | write the new value, update the Prometheus scrape secret, rollout restart | scrapes get 401 until both sides match |
| held-out salt | `SCRUPLE_HELDOUT_SALT` Secret | do not rotate casually: it seals the held-out seed namespace (`scruple.sim.splits`) and changes which seeds are held out | benchmark results before and after are not comparable |
| image signing | cosign keyless | nothing to rotate: short-lived Fulcio certificates per run | |
| registry push | `GITHUB_TOKEN` | per workflow run | |
| registry pull (private package) | `imagePullSecrets` / `registry_credentials_secret_arn` | new GitHub token with `read:packages`, update the secret | |

## Observability

### Metrics (`GET /metrics`)

Off unless configured: with `SCRUPLE_METRICS_TOKEN` it needs
`Authorization: Bearer <token>`; with `SCRUPLE_METRICS_PUBLIC=true` it is open
(only behind a NetworkPolicy or a private network); otherwise it answers 404.
The ALB in the Terraform module answers 404 for `/metrics` regardless.

| metric | type | labels |
|---|---|---|
| `scruple_http_request_duration_seconds` | histogram | method, route (template, or `unmatched`), status |
| `scruple_http_requests_in_flight` | gauge | method |
| `scruple_gate_decisions_total` | counter: committed decisions, read from the audit log at scrape (all replicas agree) | stage, outcome |
| `scruple_audit_chain_length` | gauge, read from storage at scrape | |
| `scruple_build_info` | gauge, always 1 | version, commit |

With several gunicorn workers set `PROMETHEUS_MULTIPROC_DIR` to an empty writable
directory (Helm: `/tmp/metrics`); gunicorn empties it at start and each scrape sums
all workers (tested with three workers).

### Logs

One JSON object per line on stderr (`SCRUPLE_LOG_FORMAT=json`, the image default):

| field | when |
|---|---|
| `ts` | always; UTC, ISO 8601, milliseconds |
| `level`, `logger`, `msg` | always |
| `request_id` | inside a request; from a well-formed incoming `X-Request-ID` or generated, echoed in the response |
| `method`, `path`, `status`, `duration_ms` | the access line (`logger: scruple.api`, `msg: request`), one per request |
| `trace_id`, `span_id` | tracing enabled and a span active |
| `first_bad_seq`, `problem` | `readiness: audit chain broken` (`scruple.api.health`) |
| `exc` | an exception was logged |

Gunicorn's own master lines (worker boot, SIGTERM) are plain text.

### Tracing

`SCRUPLE_OTEL_ENABLED=true` plus the standard `OTEL_EXPORTER_OTLP_ENDPOINT`
(OTLP over HTTP) gives one SERVER span per request named `METHOD /route/template`,
continuing an incoming W3C `traceparent`, with `http.route`,
`http.response.status_code` and `scruple.request_id`. Off, nothing is recorded.

## Incident response

1. **Declare.** Anyone can. Note the time, the symptom and who is handling it.
2. **Integrity first.** If `/ready` reports `audit_chain` broken, or
   `scruple audit verify --expected-head <anchored head>` fails:
   - stop deploys and stop agents writing (scale the Deployment to 0 or revoke
     their credentials);
   - snapshot the database before anything else (RDS snapshot or backup.sh);
   - do NOT restart pods to make it go away; readiness keeps them out of rotation;
   - compare against the last good backup's `.head`; the first bad position in
     the readiness detail tells you where to look;
   - treat it as a security incident until shown otherwise.
3. **Not ready, integrity fine.** Read `/ready`:
   - `database unreachable`: check the database and the NetworkPolicy/security
     groups. Pods hold one connection per worker opened at start and do not
     reconnect, so after a database failover or restart run
     `kubectl rollout restart deploy/scruple` (ECS: force a new deployment).
   - `at X, head is Y`: the migration did not run; check the Job (`kubectl logs
     job/scruple-migrate`) and re-run the release.
4. **Errors or latency over the SLO, all ready.** Was there a deploy? Roll back
   first, debug second. Check `scruple_http_requests_in_flight` (saturation:
   evaluation holds a worker for its whole run) and the HPA.
5. **After.** Write it down within five working days: timeline, impact (which
   decisions or requests), cause, what changes. Anything that touched evidence
   goes to the design partner.

## Known gaps

- Each worker holds one database connection opened at start and never reconnects
  (`scruple.storage.postgres`); after a database restart pods stay not ready
  until restarted (see Incident response).
- State that lives in one worker process, so it is per worker and per replica under
  gunicorn (`SCRUPLE_WEB_WORKERS` > 1) or several pods:
  - live shadow recorders (`/shadow/rules/.../decisions`): batches for one rule can
    land on different workers, each with its own partial summary. Keep one worker
    and one replica if you use live shadow mode. Imported shadow batches
    (`.../imported/...`) are stored and replayed from the database, so they are safe.
  - rate-limit buckets (`scruple.auth.ratelimit`, in memory): each worker counts on
    its own, so a key's effective limit is about `SCRUPLE_RATE_LIMIT_PER_MINUTE`
    times the number of workers and replicas. Set the per-key limits with that in
    mind; there is no shared bucket store yet.
  - the verified-key cache (5 minutes) and the per-tenant gate cache: harmless, since
    a key's row is read on every request and gates hold no state of their own.
- Probes: `/health`, `/ready` and `/version` are declared `public()` in the route
  guard, and `/health` and `/ready` are never rate limited (an orchestrator probing
  from one address must not get 429). `/metrics` is declared `signed("metrics-token")`:
  the auth middleware leaves its `Authorization` header alone and the route checks
  `SCRUPLE_METRICS_TOKEN` itself. `/ready` verifies every tenant's audit chain from
  that tenant's own genesis and names no tenant in its answer.
- Actions in the release workflow are pinned to version tags. Pin them to commit
  SHAs (with a bot to update them) before relying on the workflow.
- The Helm chart's rendering tests and the Terraform format test skip when helm or
  terraform are not installed (the engine CI does not install them yet).

## Validation status (2026-11-10)

| piece | what was run | not run |
|---|---|---|
| image | linux/amd64 `docker build` (with a local-only CA overlay on the build stage and a locally provided uv 0.11.32 binary, because this container's proxy blocks GHCR blobs and Debian mirrors) and run read-only with `--cap-drop ALL`: against SQLite, and against `postgres:16-alpine` with entrypoint migrations; `/metrics` summed across 2 workers with a token; tracing on (spans exported to a missing collector, trace ids on log lines); SIGTERM exits 0 in about a second | arm64 build; the Trivy scan (its database host is blocked here); the exact upstream uv image |
| gunicorn | boot with 2 and 3 workers, `/health`, graceful SIGTERM, multiprocess metrics (tests) | |
| health, metrics, tracing | unit and HTTP tests, including Postgres readiness against a real cluster | |
| release workflow | actionlint 1.7.7 (with shellcheck), YAML shape tests | never ran on GitHub; actions are pinned to major tags, not SHAs |
| Helm chart | `helm lint`, `helm template` with several value sets, kubeconform 0.6.7 strict against Kubernetes 1.30 schemas, rendering tests | never installed on a cluster |
| Terraform | `terraform validate` (1.9.8, AWS provider 5.80.0) | never planned or applied; no AWS account was used |
| backup/restore | both scripts against a local PostgreSQL 16 cluster (tests) | RDS snapshot/PITR restore |
| smoke script | against a local server, with and without a key (tests) | a real staging URL |
