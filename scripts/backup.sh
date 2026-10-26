#!/bin/sh
# Dumps the portfolio database with pg_dump. Connection details come from the usual PG*
# environment variables.
#
#   backup.sh          dump now and then every 24 hours (the `backup` service)
#   backup.sh --once   dump once and exit, non-zero if it failed (e.g. before an update)
#
# Dumps older than BACKUP_KEEP_DAYS are pruned, but only straight after a dump succeeds, so
# if backups start failing the last good ones are kept. Restore: see docs/DEPLOY.md.
set -eu

DUMP_DIR=${DUMP_DIR:-/backups}
BACKUP_KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}

# A leading zero is refused too: shell arithmetic would read 014 as octal.
case "$BACKUP_KEEP_DAYS" in
  '' | *[!0-9]* | 0*)
    echo "backup: BACKUP_KEEP_DAYS must be a whole number of days, 1 or more." >&2
    exit 1
    ;;
esac

if [ ! -d "$DUMP_DIR" ] || [ ! -w "$DUMP_DIR" ]; then
  echo "backup: cannot write to $DUMP_DIR. Check BACKUP_DIR exists and is owned by" \
    "BACKUP_UID:BACKUP_GID (see docs/DEPLOY.md)." >&2
  exit 1
fi

umask 077

dump_once() {
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  target="$DUMP_DIR/portfolio-$stamp.dump"
  if ! pg_dump --format=custom --file="$target.partial"; then
    rm -f "$target.partial"
    echo "backup: FAILED at $stamp. Older backups have been kept." >&2
    return 1
  fi
  mv "$target.partial" "$target"
  echo "backup: wrote $target"

  find "$DUMP_DIR" -maxdepth 1 -name 'portfolio-*.dump' \
    -mmin "+$((BACKUP_KEEP_DAYS * 1440))" -print -delete | sed 's/^/backup: pruned /'
  # Left behind if a dump was killed part-way through.
  find "$DUMP_DIR" -maxdepth 1 -name 'portfolio-*.dump.partial' -mmin +1440 -delete
}

if [ "${1:-}" = "--once" ]; then
  dump_once
  exit
fi

while true; do
  dump_once || true
  sleep 86400
done
