# Deploying on your server

The app runs as its own Docker Compose project, `portfolio-digest`, with its own Postgres
container. It is fully separate from anything else on the server, including any existing
Postgres 18. The dashboard publishes only a loopback or Tailscale host port; the database
is not published.

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

### Stored price convention for the returns engine (#4)

Closes are raw, unadjusted prices in the instrument's major currency units. Yahoo's
`quote.close` is retrospectively split-adjusted, so the adapter reverses all later split
ratios before storing it. Split events are fetched from the earliest stored price or first
trade through the present, even for sold holdings. A new, changed or withdrawn split
triggers a full re-read of that instrument, including rows outside the normal 31-day
refresh window.
After a successful scan, stored split rows absent from the source are removed.
Yahoo currently returns the full chart for this event scan on each run.

A 4-for-1 split therefore stores a pre-split close of $40 and a post-split close of $10.
ADJUSTMENT trades are the single source of quantity changes for the returns engine (#4),
including split units (1 share becomes 4). Split rows are used only for price un-adjustment
and quality checks. Do not apply split rows to quantities or split-adjust these closes
again. Closes are not dividend-adjusted; account for distributions separately.

Yahoo metadata currencies `GBp`, `ZAc` and `ILA` are converted to `GBP`, `ZAR` and
`ILS` by dividing by 100. Missing or mismatched quote currencies fail that source request
rather than storing a price in the wrong units. Malformed or excessively nested JSON
also fails the source request, allowing other instruments and FX fallbacks to continue.

For rows fetched before this convention was introduced, run
`portfolio-digest prices fetch --backfill` once to replace any existing adjusted closes.

### Twice-daily schedule

The checked-in systemd user timer runs at 10:30 and 18:30 in `Pacific/Auckland`, including
daylight-saving changes. The first run follows the US close and the second follows the NZX
and ASX closes. From early November to mid-March (the second Sunday in March),
the US uses standard time while NZ uses daylight time. The FX day then closes at
17:00 New York, which is 11:00 NZDT.
The 10:30 run gets US share closes, but the previous day's exchange rate only arrives
at the 18:30 run. Both scheduled times remain as decided by the owner.
Install it as the server account that owns this checkout:

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

## Private dashboard over Tailscale

The `web` service shares the app package and database. It runs FastAPI on container port
8000 with one worker. Only its host binding is published; the database remains private.
The default is `127.0.0.1:8000`. Never publish it on a wildcard or public address, configure
router forwarding, or use Tailscale Funnel.

1. Install Tailscale on the server and the devices you use to view the dashboard. Restrict
   access to trusted devices using your tailnet's access policy. Anyone allowed to reach
   this address can read financial data, including import notes.
2. Set `DASHBOARD_BIND` in the server's private `.env` to the IP printed by `tailscale ip -4`
   (normally `100.x.y.z`). Set `DASHBOARD_PASSWORD` to a long random value, at least 16
   characters, for example generated by `openssl rand -hex 32`. Keep it only in `.env`.
3. From the server checkout, run `scripts/deploy-dashboard.sh`. It validates the host IP,
   takes a backup, builds and starts the web service after migrations. The same script is
   declared as the manual `dashboard` deploy action in `.sekreton/config.toml`; it requires
   Docker, the server's existing private `.env` and backup directory on the execution host.
   No remote target or project deployment secrets are configured yet.
4. Visit `http://<server-tailscale-ip>:8000` on a trusted tailnet device. Adjust
   `DASHBOARD_PORT` if needed. Loopback, Tailscale IPv4 and Tailscale IPv6 are accepted by
   the app guard. For IPv6, use a Compose override with Docker's bracketed IPv6 binding
   syntax; `DASHBOARD_BIND` itself must remain the bare address.

For HTTPS, keep `DASHBOARD_BIND=127.0.0.1`, use
`tailscale serve --bg http://127.0.0.1:8000`, and set `DASHBOARD_SECURE_COOKIE=true` before
restarting web. Use the tailnet HTTPS URL printed by Tailscale Serve. For direct tailnet
HTTP leave `DASHBOARD_SECURE_COOKIE=false`; the transport is encrypted by Tailscale.
No reverse proxy trust is enabled, and no external CDNs, fonts or analytics are loaded.

Read pages need no sign-in. Sign in with the app password for manual trades, signed
adjustment corrections, reconciliation comparisons and import undo. Every POST checks a
CSRF token, including sign-in and sign-out. Sessions are server-side, expire after eight
hours and use HttpOnly, SameSite=Strict cookies. Five failed sign-ins in five minutes
block further attempts for the remainder of that window, across all clients. Restarting
web revokes sessions and resets this limit. Do not run multiple workers or replicas.
Rotate the password by editing `.env` and recreating web. Access logs are disabled and
password fields are never returned in error responses.

Use the CLI import commands in [IMPORTING.md](IMPORTING.md) for broker files. The dashboard
shows import status, row counts and notices and uses the same undo and reconciliation
services. Paste a complete CSV snapshot into reconciliation; results are calculated on
request and are not retained. Corrections add signed adjustment records, preserving the
original imported records and their audit trail.

### Valuation and returns integration

The base revision for dashboard issue #6 includes prices (#3) and importers (#2), but not
the returns engine (#4) or email report model (#13). `web/viewmodel.py` therefore keeps
Decimal valuation separate for later reuse/convergence. It values signed trade quantities
using the latest stored close and FX on or before each chart date. Adjustment trades are
the only source of split quantity changes. Missing data makes totals unavailable;
carried-forward dates are flagged, including normal weekends. Values exclude cash.
The chart is sampled to at most 180 points and does not measure investment performance.
Capital gain, income return, currency gain and total return remain explicitly unavailable
until the returns engine lands. Tests verify independently calculated exact valuations;
returns-engine equality cannot yet be tested against this base revision.
