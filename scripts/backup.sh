#!/bin/sh
# Dumps the database once at start-up and then every 24 hours, keeping BACKUP_KEEP_DAYS days.
# Restore with: pg_restore --clean --if-exists -d portfolio <file>.dump
set -eu

: "${BACKUP_KEEP_DAYS:=14}"
umask 077

while true; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  target="/backups/portfolio-${stamp}.dump"
  if pg_dump --format=custom --file="${target}.partial"; then
    mv "${target}.partial" "${target}"
    echo "backup: wrote ${target}"
  else
    rm -f "${target}.partial"
    echo "backup: FAILED at ${stamp}" >&2
  fi
  find /backups -name 'portfolio-*.dump' -mtime "+${BACKUP_KEEP_DAYS}" -delete
  sleep 86400
done
