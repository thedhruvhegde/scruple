#!/usr/bin/env bash
# Restore a backup made by backup.sh into an EMPTY database, then prove it.
#
#   PGPASSWORD=... deploy/scripts/restore.sh /backups/scruple-20261110T030000Z postgresql://scruple@db:5432/scruple_restore
#
# Steps: check the dump's sha256; refuse a target that already has tables;
# pg_restore in one transaction; check alembic is at the code's head; run
# `scruple audit verify` (hash chain plus registry and decision projections); check
# the chain still contains the head recorded at backup time.
# Exit 0 only if every step passed. Needs pg_restore, psql and the scruple CLI.
set -euo pipefail

[ $# -eq 2 ] || { echo "usage: $0 BACKUP_PREFIX TARGET_DATABASE_URL" >&2; exit 2; }
prefix="$1"
url="$2"
SCRUPLE="${SCRUPLE_CLI:-scruple}"
case "$url" in
  postgres://*:*@* | postgresql://*:*@*)
    echo "restore: put the password in PGPASSWORD, not in the URL" >&2
    exit 2
    ;;
  postgres://* | postgresql://*) ;;
  *) echo "restore: not a postgres URL" >&2; exit 2 ;;
esac

(cd "$(dirname "$prefix")" && sha256sum --check --quiet "$(basename "$prefix").sha256") \
  || { echo "restore: checksum mismatch for $prefix.dump" >&2; exit 1; }
echo "restore: checksum ok"

tables="$(psql "$url" -X -v ON_ERROR_STOP=1 -At -c \
  "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ "$tables" != 0 ]; then
  echo "restore: target has $tables tables in public; restore only into an empty database" >&2
  exit 1
fi

# pg_restore 17+ always starts with `SET transaction_timeout = 0`, a setting older
# servers reject. When the client tools are newer than the target server (a distro
# client against a pinned server), render the archive to SQL, drop that one line,
# and replay it through psql with the same all-or-nothing semantics.
client_major="$(pg_restore --version | sed -E 's/^[^0-9]*([0-9]+).*/\1/')"
server_major="$(psql "$url" -X -v ON_ERROR_STOP=1 -At -c \
  "SELECT current_setting('server_version_num')::int / 10000")"
if [ "$client_major" -gt "$server_major" ]; then
  echo "restore: pg_restore $client_major is newer than server $server_major; replaying through psql"
  pg_restore --no-owner --no-privileges --exit-on-error --file=- "$prefix.dump" \
    | sed -e '/^SET transaction_timeout = 0;$/d' \
    | psql "$url" -X -q -v ON_ERROR_STOP=1 --single-transaction >/dev/null
else
  pg_restore --dbname="$url" --no-owner --no-privileges --exit-on-error --single-transaction "$prefix.dump"
fi
echo "restore: pg_restore done"

want="$(python3 -c 'from scruple.storage.migrate import head; print(head())' 2>/dev/null || true)"
have="$(psql "$url" -X -v ON_ERROR_STOP=1 -At -c "SELECT version_num FROM alembic_version")"
if [ -n "$want" ] && [ "$have" != "$want" ]; then
  echo "restore: migrations at $have, code head is $want (run alembic upgrade head)" >&2
  exit 1
fi
echo "restore: migrations at ${have}"

"$SCRUPLE" audit verify --db "$url"

read -r seq hash <"$prefix.head"
found="$(psql "$url" -X -v ON_ERROR_STOP=1 -At -c \
  "SELECT entry_hash FROM audit_events WHERE seq = $((seq))")"
if [ "$found" != "$hash" ]; then
  echo "restore: audit entry $seq does not match the head recorded at backup time" >&2
  exit 1
fi
echo "restore: audit head at backup (seq $seq) present and matching"
echo "restore: OK"
