#!/bin/sh
# Container entrypoint (runs under tini).
#  1. Migrate a Postgres database to head, unless SCRUPLE_MIGRATE_ON_START=false
#     (Kubernetes: the Helm chart's pre-upgrade Job migrates instead).
#  2. `gunicorn ...` (the default CMD) runs as is; anything else is a scruple CLI
#     command, e.g. `docker run IMAGE version`.
set -eu
if [ "${SCRUPLE_MIGRATE_ON_START:-true}" = "true" ]; then
  case "${SCRUPLE_DATABASE_URL:-}" in
    postgres://*|postgresql://*) python -m scruple.storage.migrate ;;
  esac
fi
if [ "${1:-}" = "gunicorn" ]; then
  exec "$@"
fi
exec scruple "$@"
