# Deploying on your server

The app runs as its own Docker Compose project, `portfolio-digest`, with its own Postgres
container. It is fully separate from anything else on the server, including any existing
Postgres 18. No ports are published: nothing here is reachable from outside the server.

## First install

```sh
git clone https://github.com/thatskiff33/nz-portfolio-digest.git ~/portfolio-digest
cd ~/portfolio-digest
cp .env.example .env
chmod 600 .env
# Fill in both passwords with: openssl rand -hex 32
# Set BACKUP_UID and BACKUP_GID to the output of `id -u` and `id -g`.
nano .env

mkdir -p backups                                 # as your own user, before first start
docker compose up -d db backup
docker compose run --rm --build migrate          # creates the tables
docker compose run --rm migrate health           # should print "OK: ..."
docker compose run --rm migrate portfolio add "Family Trust"
docker compose run --rm migrate prices fetch --backfill
```

If you set `BACKUP_DIR` to somewhere else, create that folder instead. The `backup`
service refuses to start until the folder exists, so a typo cannot quietly send backups
nowhere.

## Prices and exchange rates

The first backfill starts at each instrument's first trade. To deliberately use a later
date, add `--start YYYY-MM-DD`. Normal runs re-read recent history so corrections from the
source replace carried-forward values. Commands are safe to repeat: an existing day is
updated, not duplicated.

```sh
docker compose run --rm migrate prices fetch --backfill
docker compose run --rm migrate prices fetch --backfill --start 2020-01-01
docker compose run --rm migrate prices fetch --instrument NZX:AIR
docker compose run --rm migrate prices check
```

Yahoo symbols are inferred for NZX (`AIR.NZ`), ASX (`BHP.AX`), NASDAQ and NYSE listings.
For a right, renamed code, delisted security or other exception, store an override on the
instrument:

```sh
docker compose run --rm migrate prices symbol NZX:AIRRG AIRRG.NZ
docker compose run --rm migrate prices symbol NZX:AIRRG --clear
```

Yahoo is unofficial, so requests retry with exponential backoff. Missing closes and rates
are carried forward and marked stale. `prices check` exits non-zero and lists missing or
stale days, zero prices, and daily moves over 50 per cent that have no recorded split.
Exchange rates use the database convention that one unit of the foreign currency equals
the stored number of NZ dollars. Yahoo is tried first and the free ECB rates from
Frankfurter are the default fallback. `PRICE_SOURCE` and `FX_SOURCES` in `.env` select the
adapters.

### Twice-daily schedule

The checked-in systemd user timer runs at 10:30 and 18:30 in `Pacific/Auckland`, including
daylight-saving changes. The first run follows the US close and the second follows the NZX
and ASX closes. Install it as the server account that owns this checkout:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/systemd/portfolio-digest-prices.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now portfolio-digest-prices.timer
systemctl --user list-timers portfolio-digest-prices.timer
```

The service expects the checkout at `~/portfolio-digest`; edit its `WorkingDirectory` if
you installed elsewhere. Enable lingering once with
`loginctl enable-linger "$USER"` if user services do not run while logged out. Inspect the
last scheduled run with
`journalctl --user -u portfolio-digest-prices.service --since today`.

## Updating

Take a fresh backup first, so a migration that goes wrong costs nothing:

```sh
cd ~/portfolio-digest
git pull
docker compose run --rm backup --once            # stop here unless it prints "backup: wrote"
docker compose up -d db backup
docker compose run --rm --build migrate
docker compose run --rm migrate health
```

## Backups

The `backup` service dumps the database when it starts and every 24 hours after that, into
`BACKUP_DIR` (default `./backups`). The files belong to `BACKUP_UID:BACKUP_GID` and only
that user can read them. Copy the folder into your normal off-server backups.

Dumps older than `BACKUP_KEEP_DAYS` (default 14) are deleted, but only straight after a
new dump succeeds. If backups start failing, the old ones stay until it is fixed, and the
service tries again every 5 minutes.

To check backups are working:

```sh
docker compose ps backup        # "healthy" means a dump was written in the last 26 hours
docker compose logs backup      # one line per dump, and the reason for any failure
```

## Restoring

Restore into the running database. `--single-transaction` means a restore that fails
part-way changes nothing. The restore runs as the `portfolio` role, which owns everything in
the dump, rather than the superuser, so nothing in a backup can run with more rights than
the app already has.

```sh
docker compose stop backup
docker compose exec -T db pg_restore -U portfolio --clean --if-exists --single-transaction \
  -d portfolio < backups/portfolio-YYYYMMDDTHHMMSSZ.dump
docker compose run --rm migrate health
docker compose start backup
```

To move to a new server, follow **First install** with the same passwords in `.env`, but
stop after `mkdir -p backups`. Then start only the database, restore as above, and carry on
from `migrate`:

```sh
docker compose up -d --wait db                   # waits until the database is ready
docker compose exec -T db pg_restore -U portfolio --clean --if-exists --single-transaction \
  -d portfolio < backups/portfolio-YYYYMMDDTHHMMSSZ.dump
docker compose up -d backup
docker compose run --rm --build migrate
docker compose run --rm migrate health
```

## Notes

- The database passwords only take effect when the `pgdata` volume is first created.
  Changing them later in `.env` needs an `ALTER ROLE` inside the database as well.
- The app and the backups log in as the `portfolio` role, which owns its own database but
  is not a superuser and cannot connect to any other database. The `postgres` superuser
  password is only given to the database container.
