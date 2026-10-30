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

### Shared price and report schedule

The `scheduler` container reads `schedules.toml`. This is the one place to change times,
report periods and recipients. Prices run at 10:30 and 18:30 Auckland time. Weekly reports
run Sunday at 20:00 for the preceding seven completed calendar days. Monthly reports run
on the 1st at 08:00 for the previous calendar month. Today's unfinished markets are excluded.
All schedules use `Pacific/Auckland`, including daylight saving. The US FX close can fall
after the morning fetch during NZ summer, so the evening fetch supplies that day's rate.

If you previously installed the price systemd timer, disable it before starting the
container, so price fetching has just one scheduler:

```sh
systemctl --user disable --now portfolio-digest-prices.timer
# After completing the mail settings below:
docker compose up -d --build scheduler
```

Edit the server copy of `schedules.toml`, then `docker compose restart scheduler`.
`recipients = []` uses `MAIL_OWNER`; otherwise supply a list of mailbox addresses, for example
`recipients = ["owner@example.invalid", "other@example.invalid"]`. It applies to every report.
Keep real addresses and private monitoring URLs in the server copy only, never commit them.
An optional `kuma_push_url` is pinged only after successful delivery. Ping failures are
logged without the URL and do not fail the send.

Each `[[schedules]]` needs a unique `name`, `kind` (`prices` or `report`), `frequency`
(`daily`, `weekly` or `monthly`) and local `at = "HH:MM"`. Weekly schedules use a lower-case
`weekday`; monthly schedules use `day` (1 to 28). Reports use `days` (1 to 3660, default 7)
or `period = "last_month"`. Unknown fields and invalid settings fail before jobs start.
A time inside the spring clock gap runs at the first valid minute afterwards. A repeated
autumn time runs once, at its first occurrence.

Pending runs live in the `scheduler-state` volume. First startup schedules future jobs;
restarts run one catch-up per schedule for a missed occurrence, retaining its original
report period, then resume future runs. Failed jobs exit non-zero, are logged and are retried
by Compose on restart. Mail delivery is at least once: a crash after provider acceptance
but before saving state can cause a duplicate. Partial recipient rejection also fails the
send; some recipients may already have received it. Check the provider before a manual retry.
Run only one scheduler replica. Inspect it with `docker compose logs scheduler`.

## Email reports

### Gmail (default)

Enable Google 2-Step Verification on the sending account. In Google Account → Security →
App passwords (or https://myaccount.google.com/apppasswords), create an app password named
"Portfolio digest". If App passwords is unavailable, the account administrator may block
it; use another mail provider. Put the generated app password, without grouping spaces,
in the server's private `.env`, never the normal account password:

```dotenv
MAILER=gmail
MAIL_FROM=digest@example.invalid
MAIL_OWNER=owner@example.invalid
GMAIL_SMTP_USERNAME=digest@example.invalid
GMAIL_APP_PASSWORD=<app password, server only>
```

Use the authenticated Gmail address as `MAIL_FROM`, or a verified Gmail send-as alias.
Delivery uses Gmail SMTP on port 587 with STARTTLS and certificate verification. Outgoing
mail settings are separate from the read-only OAuth credentials used by the ASB importer.

### Postmark

Verify the sender/domain in Postmark. Set `MAILER=postmark`, `MAIL_FROM`, `MAIL_OWNER` and
`POSTMARK_SERVER_TOKEN` (the Server API token, not the account token). Delivery uses the
HTTPS email API with both HTML and plain text. The first recipient is in To; additional
recipients are in Bcc. SMTP and SES use private envelope recipients.

### AWS SES

Verify the sender identity in your chosen SES region. In the SES sandbox, verify recipients
too, or request production access. Set `MAILER=ses`, `AWS_REGION`, `MAIL_FROM` and `MAIL_OWNER`.
For SMTP, create SES SMTP credentials and set `SES_SMTP_USERNAME` and `SES_SMTP_PASSWORD`.
These are different from IAM API keys. SMTP uses STARTTLS on port 587.

Alternatively set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, plus `AWS_SESSION_TOKEN`
for temporary credentials. Supplying the access key selects the boto3 SES API instead of
SMTP. Grant only `ses:SendRawEmail` for the sending identity. Do not set API keys when
using SES SMTP. Provider errors never print credentials or email contents.

### Other SMTP servers

Set `MAILER=smtp`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM` and `MAIL_OWNER`. Authentication
uses optional `SMTP_USERNAME` and `SMTP_PASSWORD`. `SMTP_TLS=starttls` is the default;
use `SMTP_TLS=ssl` with port 465 for implicit TLS. Certificate verification is always on.

### Send and preview

```sh
docker compose run --rm migrate report send-now --schedule weekly
# Preview locally, without loading mail credentials or making any outbound calls:
portfolio-digest report send-now --schedule weekly --dry-run --out report.html
# Docker: mount a private writable directory (owned by uid 10001) to retrieve previews:
docker compose run --rm -v "$PWD/previews:/previews" migrate report send-now \
  --schedule monthly --dry-run --out /previews/report.html
```

Dry-run writes `report.html` and `report.txt`. Keep real previews private and out of git.
The email uses inline styles and table layout, including a table-based value chart that
works without downloading images. Each market has the requested Sharesight-style columns
and subtotals, followed by the portfolio total. Missing price/FX data fails the entire
report with a non-zero exit instead of quietly omitting a holding. Stored stale data can
be used but is flagged. A failed send also exits non-zero and never pings monitoring.

The email model uses the shared period engine in `returns.py` through `returns_loader`,
documented in [RETURNS.md](RETURNS.md). It combines all portfolios by listing in NZD.
The first email date is inclusive: its opening close is the preceding day. To compare
with the CLI, use that opening date in `--period OPENING:END`. Capital, net dividends,
currency gains and total returns use exactly the same FIFO results as the CLI.
Gross dividends, withholding tax and imputation credits are shown separately;
imputation is excluded from the default total return. Foreign portfolio broker FX
is translated to NZD using stored FX for the transaction date. Split and DRP handling
also comes from the shared engine. Record DRP reinvestments at their actual price.


## Updating

Take a fresh backup first, so a migration that goes wrong costs nothing:

```sh
cd ~/portfolio-digest
git pull
docker compose run --rm backup --once            # stop here unless it prints "backup: wrote"
docker compose up -d db backup
docker compose run --rm --build migrate
docker compose run --rm migrate health
docker compose up -d --build scheduler
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
docker compose up -d --build scheduler
```

## Notes

- The database passwords only take effect when the `pgdata` volume is first created.
  Changing them later in `.env` needs an `ALTER ROLE` inside the database as well.
- The app and the backups log in as the `portfolio` role, which owns its own database but
  is not a superuser and cannot connect to any other database. The `postgres` superuser
  password is only given to the database container.

## Dashboard upgrade note

Existing servers can keep deploying, migrating, scheduling and backing up without
setting `DASHBOARD_PASSWORD`. Compose defaults it to an empty value. Before starting
the optional dashboard, set a random password of at least 16 characters in the
server's private `.env`. The dashboard refuses to start with a missing or shorter
password.

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
   For remote server deployment, see the optional `server` action below.
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
hours and use HttpOnly, SameSite=Strict cookies. Anonymous read pages do not create
sessions; opening the sign-in form creates one for its CSRF token. Five failed sign-ins
in five minutes block that client address for the remainder of that window. A global
ceiling of 50 failures in five minutes limits attempts across all clients. Restarting
web revokes sessions and resets these limits. Do not run multiple workers or replicas.
Rotate the password by editing `.env` and recreating web. Access logs are disabled and
password fields are never returned in error responses.

Use the CLI import commands in [IMPORTING.md](IMPORTING.md) for broker files. The dashboard
shows import status, row counts and notices and uses the same undo and reconciliation
services. Paste a complete CSV snapshot into reconciliation; results are calculated on
request and are not retained. Corrections add signed adjustment records, preserving the
original imported records and their audit trail.

### Valuation and returns integration

The dashboard loads the same stored inputs as the CLI returns report and uses the #4
returns engine for opening and closing values, capital gain, gross dividends, currency
gain, total return and percentages, including market and portfolio totals. Periods use
the engine's opening-close boundaries (see [RETURNS.md](RETURNS.md)). Missing inputs
make the selected period's figures unavailable with an explanation; carried-forward
observations use the engine's trading-calendar flags. All calculations use Decimal.
The value chart samples at most 180 closing valuations calculated by the same engine.
Values exclude cash; changes in value include trades and are not investment returns.

## Optional Sekreton deployment action

After first install, the manual `server` action in `.sekreton/config.toml` runs
`scripts/deploy.sh` over SSH. The server must already have Docker Compose, a `main`
checkout, its private `.env`, and a working backup directory. Disable the old systemd
price timer as described above before the first action. Under Settings → Deploy & Publish,
set `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_CHECKOUT` (absolute path without spaces),
`DEPLOY_SSH_KEY` and `DEPLOY_KNOWN_HOSTS` (a host key verified independently).
The deployment identity needs access to that checkout and Docker. Mail and database
credentials stay in the server's `.env`; the action does not receive them.

The script preserves the server's schedule configuration, fast-forwards `main`, takes a
backup, builds the app, stops the scheduler while migrating, checks database health and
starts the services. If a migration fails, the scheduler stays stopped for investigation.
Deployment does not run automatically on merge.

### Price and movement alerts

Apply the database migration before starting the updated scheduler:

```bash
docker compose run --rm migrate migrate
docker compose up -d --build scheduler
docker compose run --rm migrate alerts check --dry-run
```

The existing scheduler checks alerts immediately after every scheduled price fetch,
including partial fetches. Manual `prices fetch` does not send alerts. Use
`alerts check` for an explicit check. No additional timer or scheduler is needed.
The dry-run prints only alerts currently due and neither sends notifications nor changes
suppression state. Its terminal output contains portfolio figures, so keep it private.

Set these rules in the server's `schedules.toml`, then restart the scheduler:

```toml
[alerts]
enabled = true
daily_percent = "10"
portfolio_drop_percent = "5"
period_days = 7
stale_trading_days = 3
# Optional repeat interval while a rule remains triggered:
# cooldown_hours = 24

[alerts.holdings."NZX:EXAMPLE"]
daily_percent = "15"
# Other holding thresholds inherit the global values.
```

Threshold comparisons are strict: exactly 10% does not exceed a 10% rule. Daily
movement compares fresh closes on adjacent completed trading days in the holding's
listing currency, adjusted for splits. Fees, trades and currency movements do not
change this price comparison. Missing closes leave its suppression state unchanged.
Staleness counts completed exchange trading days, excluding weekends and exchange
holidays. If no fresh price exists, it counts from the first trade. Sold holdings do
not trigger holding alerts.

Portfolio value uses the shared returns engine's opening and closing valuations in
the portfolio's base currency across the configured calendar-day period. The period
ends on the most recent date completed by all portfolio markets and, when needed,
the FX market, so a close that is not yet due does not suppress the check. This is a
value-drop rule: sales and withdrawals can reduce it, purchases can offset falls,
and it is not a cash-flow-adjusted performance percentage. Incomplete or stale
valuations suppress that evaluation rather than inventing figures.

Emails list the triggering figures and a reminder to open the private dashboard over
Tailscale. They reuse `MAILER`, sender and `recipients` (or `MAIL_OWNER` when empty).
Delivery state lives in the database and survives scheduler restarts. Each portfolio
and rule sends once until a successful evaluation clears it; optional `cooldown_hours`
allows repeats while it remains triggered. Failed email delivery is retried without
recording a sent alert. A process failure after email delivery but before the database
commit can cause a duplicate on retry, since email and the database cannot commit
atomically.

Push is off by default. In the server's private `.env`, set `ALERT_NTFY_URL` to an HTTPS
topic URL and optionally `ALERT_NTFY_TOKEN`, or set both `ALERT_PUSHOVER_TOKEN` and
`ALERT_PUSHOVER_USER`. ntfy takes precedence if both providers are configured. Recreate
the scheduler container after changing environment values. Push providers receive the
same portfolio figures as the email; use only a private topic or account. Never put
URLs or tokens in TOML, commands or git. Push requests refuse redirects to protect tokens and portfolio figures.
Push failures emit a generic warning without
secrets and do not retry an already successful email.
