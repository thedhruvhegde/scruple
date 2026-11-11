#!/usr/bin/env bash
# Rebuild the hosted demo from scratch: stop the API, seed a fresh synthetic "demo"
# tenant (seed.sh), start the API, smoke test it with the new read-only key.
# Run from deploy/demo on the demo VM; the systemd timer in systemd/ runs it nightly.
#
#   ./reset.sh              # uses demo.env next to this script
#
# This DELETES the demo database and its audit history on purpose. That is only
# acceptable because everything in it is synthetic and re-creatable. Never point this
# script, or anything like it, at a database that holds real evidence.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cd "$here"
env_file=${DEMO_ENV_FILE:-$here/demo.env}
[ -f "$env_file" ] || { echo "reset: missing $env_file (copy demo.env.example)" >&2; exit 2; }
compose=(docker compose --env-file "$env_file" -f "$here/compose.yaml")

# The seed job runs as uid 10001; the key file must not be readable by anyone else.
install -d -m 0700 out
chown 10001:10001 out 2>/dev/null || true

old_key_id=""
if [ -f out/visitor-key.json ]; then
  old_key_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["key_id"])' out/visitor-key.json || true)
fi

echo "reset: seeding (the API keeps serving yesterday's demo until the swap)"
"${compose[@]}" run --rm --no-deps seed build
"${compose[@]}" stop api
"${compose[@]}" run --rm --no-deps seed swap
"${compose[@]}" up -d --wait api caddy

key=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_key"])' out/visitor-key.json)
new_key_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["key_id"])' out/visitor-key.json)
domain=$(sed -n 's/^DEMO_DOMAIN=//p' "$env_file")

# Smoke: health, readiness, the key reads rules, and a write is refused at the edge.
base="https://$domain"
curl -fsS "$base/health" >/dev/null
curl -fsS "$base/ready" >/dev/null
curl -fsS -H "Authorization: Bearer $key" "$base/rules" >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $key" "$base/rules")
[ "$code" = "405" ] || { echo "reset: a write was not refused at the edge ($code)" >&2; exit 1; }
curl -fsS -H "Authorization: Bearer $key" "$base/audit/verify" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); sys.exit(0 if r["ok"] else 1)'

echo "reset: OK. visitor key $new_key_id (previous ${old_key_id:-none} is gone with the old database)"
