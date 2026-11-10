#!/usr/bin/env bash
# Logical backup of a scruple PostgreSQL database.
#
#   PGPASSWORD=... deploy/scripts/backup.sh postgresql://scruple@db:5432/scruple /backups/scruple-$(date -u +%Y%m%dT%H%M%SZ)
#
# Writes three files next to each other:
#   <out>.dump    pg_dump custom format (compressed; restore with restore.sh)
#   <out>.sha256  checksum of the dump
#   <out>.head    the audit chain head (seq and entry_hash) at the time of the dump.
#                 Store it apart from the dump: restore.sh checks the restored chain
#                 against it, so a dump edited after the fact is caught.
# The password is never an argument: libpq reads PGPASSWORD or ~/.pgpass.
set -euo pipefail

[ $# -eq 2 ] || { echo "usage: $0 DATABASE_URL OUT_PREFIX" >&2; exit 2; }
url="$1"
out="$2"
case "$url" in
  postgres://*:*@* | postgresql://*:*@*)
    echo "backup: put the password in PGPASSWORD, not in the URL" >&2
    exit 2
    ;;
  postgres://* | postgresql://*) ;;
  *) echo "backup: not a postgres URL" >&2; exit 2 ;;
esac
for f in "$out.dump" "$out.sha256" "$out.head"; do
  [ ! -e "$f" ] || { echo "backup: $f exists, refusing to overwrite" >&2; exit 1; }
done

# Head first, dump second. The audit log is append-only, so the dump can only hold
# the same entries or more; restore.sh checks this recorded entry is in it, intact.
psql "$url" -X -v ON_ERROR_STOP=1 -At -c \
  "SELECT seq || ' ' || entry_hash FROM audit_events ORDER BY seq DESC LIMIT 1" >"$out.head"

pg_dump "$url" --format=custom --compress=6 --no-owner --no-privileges --file="$out.dump"
(cd "$(dirname "$out")" && sha256sum "$(basename "$out").dump") >"$out.sha256"

echo "backup: $out.dump ($(wc -c <"$out.dump") bytes), audit head $(cut -c1-24 "$out.head")"
