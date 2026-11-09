#!/bin/sh
# Migrate a Postgres database to head, then run the scruple CLI (default: serve).
set -eu
case "${SCRUPLE_DATABASE_URL:-}" in
  postgres://*|postgresql://*) python -m scruple.storage.migrate ;;
esac
exec scruple "$@"
