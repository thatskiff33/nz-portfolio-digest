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
# First install is documented separately. This action always preserves a fresh backup.
docker compose run --rm backup --once
docker compose build migrate scheduler
docker compose stop scheduler
# PR #29 used a separate annual timer. Retire it before the shared scheduler starts.
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
docker compose up -d db backup scheduler
REMOTE
