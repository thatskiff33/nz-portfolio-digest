#!/bin/sh
# Run from Sekreton Deploy & Publish against an already provisioned Compose host.
set -eu
umask 077
: "${DEPLOY_HOST:?Set DEPLOY_HOST}"
: "${DEPLOY_USER:?Set DEPLOY_USER}"
: "${DEPLOY_CHECKOUT:?Set DEPLOY_CHECKOUT}"
: "${DEPLOY_SSH_KEY:?Set DEPLOY_SSH_KEY}"
: "${DEPLOY_KNOWN_HOSTS:?Set DEPLOY_KNOWN_HOSTS}"

# These become remote shell arguments. Limit them to unambiguous host/user/path syntax.
case "$DEPLOY_HOST" in *[!a-zA-Z0-9.-]*|'') exit 2;; esac
case "$DEPLOY_USER" in *[!a-zA-Z0-9_-]*|-*|'') exit 2;; esac
case "$DEPLOY_CHECKOUT" in /*) ;; *) exit 2;; esac
case "$DEPLOY_CHECKOUT" in *[!a-zA-Z0-9/_.-]*) exit 2;; esac

mkdir -p /tmp/scratch
deploy_scratch=$(mktemp -d /tmp/scratch/portfolio-deploy.XXXXXX)
trap 'rm -rf "$deploy_scratch"' EXIT HUP INT TERM
printf '%s\n' "$DEPLOY_SSH_KEY" > "$deploy_scratch/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$deploy_scratch/known_hosts"
ssh -i "$deploy_scratch/key" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$deploy_scratch/known_hosts" \
    "$DEPLOY_USER@$DEPLOY_HOST" "sh -s -- '$DEPLOY_CHECKOUT'" <<'REMOTE'
set -eu
cd "$1"
test "$(git branch --show-current)" = main
git diff --quiet -- . ":!schedules.toml"
git diff --cached --quiet
previous_release=$(git rev-parse HEAD)
previous_revision=$(docker compose exec -T db psql -U portfolio -d portfolio -Atc \
    'SELECT version_num FROM alembic_version')
backup_result=$(docker compose run --rm backup --once)
printf '%s\n' "$backup_result"
# Record recovery metadata before changing the release. Keep this beside the dump.
umask 077
# BACKUP_DIR is normally configured in .env; use Compose's mounted backup directory
# for metadata too, without sourcing .env or printing its values.
# Expand the metadata filename inside the backup container.
# shellcheck disable=SC2016
docker compose run --rm --entrypoint sh backup -c '
    umask 077
    cat > "/backups/pre-update-$(date -u +%Y%m%dT%H%M%SZ).txt"
' <<EOF
$previous_release
$previous_revision
$backup_result
EOF
# The private server schedule is backed up and restored around the fast-forward update.
umask 077
mkdir -p /tmp/scratch
schedule_backup=$(mktemp /tmp/scratch/portfolio-schedule.XXXXXX)
cp schedules.toml "$schedule_backup"
trap 'cp "$schedule_backup" schedules.toml; rm -f "$schedule_backup"' EXIT HUP INT TERM
git restore schedules.toml
git pull --ff-only origin main
cp "$schedule_backup" schedules.toml
rm -f "$schedule_backup"
trap - EXIT HUP INT TERM
# Compose includes stopped containers with --all. Never execute the private .env.
web_enabled=false
web_container=$(docker compose ps --all --quiet web)
if [ -n "$web_container" ]; then
    web_enabled=true
elif [ -f .env ] && awk '
    /^[[:space:]]*(export[[:space:]]+)?DASHBOARD_PASSWORD[[:space:]]*=/ {
        value = $0
        sub(/^[^=]*=[[:space:]]*/, "", value)
        sub(/[[:space:]]+$/, "", value)
        quote = substr(value, 1, 1)
        if (quote == "\042" || quote == "\047") {
            value = substr(value, 2)
            end = index(value, quote)
            if (end) value = substr(value, 1, end - 1)
        } else {
            sub(/[[:space:]]+#.*/, "", value)
            if (value ~ /^#/) value = ""
        }
        enabled = length(value) > 0
    }
    END { exit !enabled }
' .env; then
    web_enabled=true
fi
set -- migrate scheduler
if [ "$web_enabled" = true ]; then set -- "$@" web; fi
docker compose build "$@"
docker compose stop scheduler web
# Retire the standalone year-end timer used before November 2026.
if command -v systemctl >/dev/null 2>&1 &&
    systemctl --user list-unit-files portfolio-digest-year-end.timer --no-legend |
        grep -q '^portfolio-digest-year-end.timer'; then
    systemctl --user disable --now portfolio-digest-year-end.timer
    rm -f "$HOME/.config/systemd/user/portfolio-digest-year-end.service" \
        "$HOME/.config/systemd/user/portfolio-digest-year-end.timer"
    systemctl --user daemon-reload
fi
docker compose run --rm migrate
docker compose run --rm migrate health
set -- db backup scheduler
if [ "$web_enabled" = true ]; then set -- "$@" web; fi
docker compose up -d --no-deps "$@"
REMOTE
