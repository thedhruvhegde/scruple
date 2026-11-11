# Hosted demo: one VM, one region, synthetic data only

A public, read-only scruple API that prospects can query with a shared key, rebuilt
from scratch every night. Nothing in it comes from a partner or a customer: every case,
decision and audit entry is produced by the simulator from `docs/pilot/demo/*.json`.

## Status

| piece | checked here | not checked |
|---|---|---|
| `compose.yaml` | `docker compose config` (Compose v5.6.1), shape test in `tests/test_deploy_demo.py` | never `up` on a VM: this container has no Docker daemon |
| `Caddyfile` | `caddy validate` and `caddy fmt` (Caddy 2.8.4); ran it on plain HTTP in front of `scruple serve` and checked GET 200, POST 405, `/metrics` and `/auth/keys` 404, no key 401, `Authorization` redacted in the access log | ACME issuance (needs a real domain); the `caddy:2.8.4-alpine` image itself |
| `seed.sh` | ran end to end against this checkout's CLI, in a test that then serves the database and checks the key (read 200, write 403) | inside the image as uid 10001 |
| `reset.sh` | `bash -n`, shellcheck | never run: it needs Docker and a domain |
| systemd units | written by hand | `systemd-analyze verify` on a VM |

No cloud account was used to write or check any of this.

## What runs

```
internet ──443──► caddy (TLS, GET/HEAD only, /metrics and /auth/keys closed)
                    └──► api:8000  (scruple image, SQLite /data/demo.db, 1 worker)
nightly timer ──► reset.sh ──► seed (one-shot, same image) ──► swap ──► restart api
```

- One VM in one region. Pick the smallest instance with 1 GB of memory or more that
  your provider offers; the API is limited to 768 MB and Caddy to 256 MB in the
  compose file. Any Linux with Docker Engine and the Compose plugin works.
- SQLite on a named volume: the demo has one writer (the seed job) and one reader
  process, which is what SQLite is good at. No Postgres to run or pay for.
- Only Caddy publishes ports (80, 443). The API is reachable from Caddy only.

## Read-only, three ways

1. **The key's role.** `seed.sh` mints one key for tenant `demo` with the `auditor`
   role: permission `read` only (`scruple/auth/rbac.py`). A write with it gets 403.
2. **The edge.** Caddy answers 405 to anything but GET and HEAD before the API sees it.
3. **The data.** Every night the database is replaced, so whatever happened during the
   day (nothing should) is gone by morning.

Other settings in `compose.yaml`: per-key rate limit `DEMO_RATE_PER_MINUTE` (default
60), a 16 KB body cap, no metrics token and metrics not public (so `/metrics` is 404
even past Caddy), the development `X-Actor` mode off.

## The visitor key

- Written once per reset to `deploy/demo/out/visitor-key.json` on the VM (mode 0600,
  gitignored). Only the scrypt hash is in the database.
- Expires after `DEMO_KEY_DAYS` (default 2); each reset makes a new one and the old one
  disappears with the old database.
- It reads synthetic data only, so it can be handed out in an email or shown on a
  slide. Do not reuse this flow for a pilot tenant; pilot keys are per person
  (docs/pilot/SECURITY.md).

What a visitor can do with it:

```bash
KEY=scr_...                       # from out/visitor-key.json
BASE=https://demo.example.com     # your DEMO_DOMAIN
curl -H "Authorization: Bearer $KEY" $BASE/auth/whoami
curl -H "Authorization: Bearer $KEY" $BASE/rules
curl -H "Authorization: Bearer $KEY" $BASE/decisions
curl -H "Authorization: Bearer $KEY" $BASE/decisions/dec-00004/report
curl -H "Authorization: Bearer $KEY" $BASE/audit/verify
```

## First deploy

```bash
# on the VM, as a user in the docker group, with the engine checked out at
# /opt/scruple/engine (the systemd unit assumes that path)
cd /opt/scruple/engine/deploy/demo
cp demo.env.example demo.env      # set DEMO_DOMAIN and ACME_EMAIL
docker compose --env-file demo.env build
./reset.sh                        # seed, start, smoke test over HTTPS
sudo cp systemd/scruple-demo-reset.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scruple-demo-reset.timer
```

Point the domain's A/AAAA record at the VM before the first `reset.sh`: Caddy needs
it to get a certificate, and the smoke test talks HTTPS to the domain.

Before the first deploy, pin `caddy:2.8.4-alpine` by digest in `compose.yaml`
(`docker buildx imagetools inspect caddy:2.8.4-alpine`); the registry rate-limited the
lookup when this was written, so it is pinned by tag only.

## Nightly reset

`reset.sh`:

1. `seed build`: builds `demo.db.new` in the volume while the API keeps serving
   yesterday's database; the new chain must pass `scruple audit verify`.
2. stops the API, `seed swap` moves the new file into place (and drops any SQLite
   journal left by the old one), starts the API and Caddy.
3. smoke test over HTTPS: `/health`, `/ready`, a read with the new key, a POST that
   must get 405, `/audit/verify` must say ok.

If step 1 fails, nothing changed. If step 3 fails the unit fails; `journalctl -u
scruple-demo-reset` shows why. The demo is down for the few seconds between stop and
start.

This deletes the demo's history on purpose. That is only acceptable because it is
synthetic. Never run `reset.sh` or anything like it against a database holding real
evidence; real deployments use docs/DEPLOY.md (append-only, backups, restore drills).

## Updating the code

```bash
cd /opt/scruple/engine && git pull
cd deploy/demo && docker compose --env-file demo.env build && ./reset.sh
```

## Costs and limits

- No price is quoted here; it depends on the provider and region you pick.
- One worker, one VM, no redundancy. If the VM is down, the demo is down; it is a demo.
- Shadow recorders and rate-limit buckets live in the worker's memory (docs/DEPLOY.md
  "Known gaps"); with one worker that is exact.
- No backups: the nightly reset is the recovery plan.
