#!/usr/bin/env bash
# Smoke test a running scruple engine (staging, a fresh deploy, a local container).
#
#   scripts/smoke.sh https://scruple.staging.example.internal
#   SCRUPLE_SMOKE_API_KEY=... scripts/smoke.sh http://127.0.0.1:8000
#
# Read-only checks, always: /health is 200, /ready is 200 with every check ok,
# /version names a build, /metrics is not open without credentials (unless
# SCRUPLE_SMOKE_METRICS_PUBLIC=1).
#
# With SCRUPLE_SMOKE_API_KEY set it also WRITES: it registers a throwaway rule
# smoke-<unix time> (escalate, matches nothing real) and evaluates it, which records
# decisions in the append-only audit log. Run that part against staging, not prod.
# The key is a scruple API key with the `proposer` role (`scruple keys create
# --subject smoke-test --role proposer`), sent as `Authorization: Bearer <key>`.
# Identity comes from the key: /auth/whoami names its subject, which the script uses
# as the rule's owner and provenance agent (the API requires the caller to be the
# provenance agent). No X-Actor header is sent; the server ignores it outside the
# development-only mode.
#
# Optional: SCRUPLE_SMOKE_EXPECT_COMMIT=<sha prefix> fails unless /version matches.
# Exit status: 0 all passed, 1 a check failed, 2 usage. Needs curl and python3.
set -euo pipefail

usage() {
  echo "usage: $0 BASE_URL" >&2
  exit 2
}

[ $# -eq 1 ] || usage
BASE="${1%/}"
case "$BASE" in
  http://* | https://*) ;;
  *) usage ;;
esac

KEY="${SCRUPLE_SMOKE_API_KEY:-}"
TIMEOUT="${SCRUPLE_SMOKE_TIMEOUT:-10}"
EVAL_TIMEOUT="${SCRUPLE_SMOKE_EVAL_TIMEOUT:-180}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

failures=0
pass() { printf 'ok    %s\n' "$1"; }
fail() {
  printf 'FAIL  %s\n' "$1" >&2
  failures=$((failures + 1))
}

# request METHOD PATH [curl args...] -> sets $status, body in $WORK/body
request() {
  local method="$1" path="$2"
  shift 2
  status="$(curl -sS -o "$WORK/body" -w '%{http_code}' -X "$method" \
    --max-time "$TIMEOUT" "$@" "$BASE$path" 2>"$WORK/err")" || status="000"
}

json() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {}, {'d': d}))" "$WORK/body" "$1" 2>/dev/null; }

# 1. liveness
request GET /health
if [ "$status" = 200 ] && [ "$(json 'd["status"]')" = ok ]; then pass "health"; else fail "health: HTTP $status $(cat "$WORK/err")"; fi

# 2. readiness, every check named
request GET /ready
if [ "$status" = 200 ] && [ "$(json 'd["status"]')" = ready ]; then
  pass "ready ($(json '", ".join(k + ": " + v["detail"] for k, v in d["checks"].items())'))"
else
  fail "ready: HTTP $status $(cat "$WORK/body" 2>/dev/null)"
fi

# 3. version
request GET /version
if [ "$status" = 200 ]; then
  commit="$(json 'd["commit"]')"
  pass "version $(json 'd["version"]') commit ${commit:0:12}"
  if [ -n "${SCRUPLE_SMOKE_EXPECT_COMMIT:-}" ] && [[ "$commit" != "${SCRUPLE_SMOKE_EXPECT_COMMIT}"* ]]; then
    fail "version: expected commit ${SCRUPLE_SMOKE_EXPECT_COMMIT}, got $commit"
  fi
else
  fail "version: HTTP $status"
fi

# 4. /metrics must not be open to anonymous callers
request GET /metrics
if [ "${SCRUPLE_SMOKE_METRICS_PUBLIC:-0}" = 1 ]; then
  if [ "$status" = 200 ]; then pass "metrics (public)"; else fail "metrics: HTTP $status"; fi
elif [ "$status" = 401 ] || [ "$status" = 404 ]; then
  pass "metrics closed to anonymous callers (HTTP $status)"
else
  fail "metrics answered HTTP $status without credentials"
fi

# 5. authenticated write path (staging only)
if [ -n "$KEY" ]; then
  auth=(-H "Authorization: Bearer $KEY")
  request GET /auth/whoami "${auth[@]}"
  ACTOR=""
  if [ "$status" = 200 ]; then
    ACTOR="$(json 'd["subject"]')"
    pass "whoami $ACTOR (tenant $(json 'd["tenant"]'), $(json '",".join(d["permissions"])'))"
  else
    fail "whoami: HTTP $status $(head -c 300 "$WORK/body")"
  fi
fi
if [ -n "$KEY" ] && [ -n "$ACTOR" ]; then
  rule_id="smoke-$(date +%s)"
  cat >"$WORK/rule.json" <<JSON
{"rule_id": "$rule_id", "owner": "$ACTOR",
 "provenance": {"agent_id": "$ACTOR", "notes": "scripts/smoke.sh"},
 "spec": {"description": "smoke test rule, safe to ignore",
          "conditions": [{"field": "amount_usd_minor", "op": "gt", "value": 999999999999}],
          "action": "escalate", "priority": 1000}}
JSON
  request POST /rules "${auth[@]}" -H 'Content-Type: application/json' --data @"$WORK/rule.json"
  if [ "$status" = 201 ]; then
    pass "register $rule_id"
    TIMEOUT="$EVAL_TIMEOUT" request POST "/rules/$rule_id/versions/1/evaluate" "${auth[@]}"
    if [ "$status" = 200 ]; then
      pass "evaluate $rule_id: $(json '", ".join(x["stage"] + "=" + x["outcome"] for x in d)')"
    else
      fail "evaluate: HTTP $status $(head -c 300 "$WORK/body")"
    fi
  else
    fail "register: HTTP $status $(head -c 300 "$WORK/body")"
  fi
elif [ -z "$KEY" ]; then
  echo "skip  evaluate (set SCRUPLE_SMOKE_API_KEY to run the write path)"
fi

if [ "$failures" -gt 0 ]; then
  echo "smoke: $failures check(s) failed against $BASE" >&2
  exit 1
fi
echo "smoke: all checks passed against $BASE"
