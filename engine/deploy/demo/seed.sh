#!/bin/sh
# Seed the hosted demo's "demo" tenant with synthetic data, then mint the read-only
# visitor key. Runs inside the scruple image (compose service `seed`), as uid 10001,
# while the API is stopped; reset.sh calls it. Also runs on a laptop for a dry run:
#   SCRUPLE_BIN="uv run scruple" DEMO_DATA=/tmp/demo DEMO_RULES=docs/pilot/demo \
#     DEMO_OUT=/tmp/demo-out sh deploy/demo/seed.sh
#
# What it builds (all synthetic, from the simulator; no partner or customer data):
#   - the baseline policy (imported on first use)
#   - inr-delivered-contest v1: shadow promote, canary pass, approved by the demo
#     approver model-risk@demo.bank (a placeholder address, not a person)
#   - inr-delivered-contest-fast v1: blocked at shadow (the reward-hacking candidate)
#   - an auditor key (read only) for visitors, expiring after DEMO_KEY_DAYS
#
#   seed.sh build   build $DATA/demo.db.new and the key (the API may keep running)
#   seed.sh swap    move demo.db.new over demo.db (run only while the API is stopped)
# The new database replaces the old one only after its audit chain verifies, so a
# failed seed leaves yesterday's demo untouched.
set -eu

SCRUPLE=${SCRUPLE_BIN:-scruple}
DATA=${DEMO_DATA:-/data}
RULES=${DEMO_RULES:-/seed/rules}
OUT=${DEMO_OUT:-/out}
DAYS=${DEMO_KEY_DAYS:-2}
APPROVER=model-risk@demo.bank

NEW="$DATA/demo.db.new"
if [ "${1:-build}" = "swap" ]; then
  [ -f "$NEW" ] || { echo "seed: nothing to swap ($NEW missing)" >&2; exit 1; }
  rm -f "$DATA/demo.db-journal" "$DATA/demo.db-wal" "$DATA/demo.db-shm"
  mv "$NEW" "$DATA/demo.db"
  echo "seed: swapped in the new demo database"
  exit 0
fi
rm -f "$NEW" "$NEW-journal" "$NEW-wal" "$NEW-shm"
export SCRUPLE_DATABASE_URL="$NEW" SCRUPLE_TENANT=demo SCRUPLE_LOG_FORMAT=text

$SCRUPLE propose "$RULES/good.json"
$SCRUPLE evaluate inr-delivered-contest 1
$SCRUPLE approve inr-delivered-contest 1 --approver "$APPROVER"
$SCRUPLE propose "$RULES/bad.json"
$SCRUPLE evaluate inr-delivered-contest-fast 1
$SCRUPLE audit verify

umask 077
$SCRUPLE keys create --tenant demo --subject demo-visitor --role auditor \
  --expires-days "$DAYS" --description "hosted demo visitor key (read only, synthetic data)" \
  --json >"$OUT/visitor-key.json.new"
mv "$OUT/visitor-key.json.new" "$OUT/visitor-key.json"
echo "seed: built $NEW (tenant demo, key in $OUT/visitor-key.json); run 'seed.sh swap' next"
