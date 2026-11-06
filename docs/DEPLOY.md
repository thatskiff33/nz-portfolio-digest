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
on the 1st at 08:00 for the previous calendar month. Year-end summaries run on 1 April
at 09:00 for the NZ tax year just ended, one email per NZD portfolio. Weekly and monthly
reports exclude today's unfinished markets.
All schedules use `Pacific/Auckland`, including daylight saving. The US FX close can fall
after the morning fetch during NZ summer, so the evening fetch supplies that day's rate.

If you previously installed the price systemd timer or PR #29's year-end timer, disable
them before starting the container, so each job has just one scheduler:

```sh
systemctl --user disable --now portfolio-digest-prices.timer
# Only if the old year-end timer was installed:
systemctl --user disable --now portfolio-digest-year-end.timer
rm -f ~/.config/systemd/user/portfolio-digest-year-end.{service,timer}
systemctl --user daemon-reload
# After completing the mail settings below:
docker compose up -d --build scheduler
```

Edit schedule times in `schedules.toml`, then `docker compose restart scheduler`.
Keep private report values in the server `.env`: `REPORT_RECIPIENTS` accepts a
comma-separated list of mailbox addresses, and `REPORT_KUMA_PUSH_URL` holds the optional
monitoring push URL. Non-empty environment values override their TOML equivalents.
Empty or whitespace-only environment settings leave TOML values in effect; if the resulting
recipient list is empty, reports use `MAIL_OWNER`. This applies to every report.

To migrate an existing server, move recipients and the monitoring URL from its
`schedules.toml` into these `.env` settings, restore `recipients = []`, and remove
`kuma_push_url` from TOML. Recreate containers after environment changes with
`docker compose up -d --force-recreate scheduler`. Legacy TOML values still work;
any TOML `kuma_push_url` emits a warning recommending `REPORT_KUMA_PUSH_URL`, even when
an environment value overrides it. The warning never includes the URL.
The deploy script continues preserving existing server schedules for compatibility.
Monitoring is pinged only after successful delivery. Ping failures are logged without
the URL and do not fail the send.

An optional `REPORT_PRICES_KUMA_PUSH_URL` in the server `.env` is pinged only after a
scheduled price fetch in which every instrument and rate succeeded. Give that Push monitor
a 26-hour heartbeat. Prices run twice a day, so one missed run stays quiet and a day
without a clean fetch raises an alert. A non-empty value overrides any legacy TOML
`prices_kuma_push_url`, which also emits a warning without the URL. Ping failures are
logged without the URL and do not fail the job.

Each `[[schedules]]` needs a unique `name`, `kind` (`prices` or `report`), `frequency`
(`daily`, `weekly`, `monthly` or `yearly`) and local `at = "HH:MM"`. Weekly schedules use a
lower-case `weekday`; monthly schedules use `day` (1 to 28). Reports use `days` (1 to 3660, default 7)
or `period = "last_month"`. Yearly schedules use `month` (1 to 12) and `day` (1 to 28).
The annual summary uses `period = "tax_year"`. Unknown fields and invalid settings fail
before jobs start.
A time inside the spring clock gap runs at the first valid minute afterwards. A repeated
autumn time runs once, at its first occurrence.

Pending runs live in the `scheduler-state` volume. First startup schedules future jobs;
restarts run one catch-up per schedule for a missed occurrence, retaining its original
report period, then resume future runs. Intervening skipped occurrences are logged.
Each failed job stays pending while other schedules continue. Retries start 30 minutes
after the failed attempt finishes, then wait 60, 120, 240 and at most 360 minutes.
Retry times, failure counts and successful portfolio deliveries are saved in the existing
JSON file and survive restarts. A partial instrument price fetch advances with warnings;
if every instrument fails, the price job retries even when FX succeeds. No holdings counts
as success. Alerts still evaluate committed data after partial fetches.
Only configuration or scheduler-state errors stop the process with a non-zero exit.
State writes flush and sync the temporary file before replacing the saved state.
Changing a schedule's timing or kind, or removing it, discards its pending occurrence
with a log entry; unrelated configuration fields preserve it.

A failed job, including a price fetch that finishes with failed
instruments, also sends one operations email through the configured mailer to the report
recipients, or to `MAIL_OWNER` when that list is empty. The optional ntfy or Pushover
push used for alerts is included when it is set. The message names the job and the failing
instrument labels, with the date, and leaves out balances, prices and links. Digits in
job and instrument names are spelt out, so the date is the only numeric text. While the
failure continues, that email is sent at most once every 24 hours. The throttle is stored
in the scheduler state file, next to the pending run the scheduler retries. The next successful
run sends one recovered notice. If the notice cannot be sent, that is logged and the
scheduler continues its delayed retries. Notices are on whenever the mailer is
configured. The `migrate` service mounts the same state volume, so a manual `prices fetch`
or the Akahu sync can persist its once-a-day limit there too. Overlapping standalone
commands share a lock; successful manual fetches also send a recovery notice.

Mail delivery is at least once: a crash after provider acceptance but before saving
state or committing the database can cause a duplicate. Partial recipient rejection
also fails the send; some recipients may already have received it. Check the provider
before a manual retry. Keep the state volume and run only one scheduler replica.
Inspect it with `docker compose logs scheduler`.
The scheduler writes `/tmp/scheduler-heartbeat` every time around its loop. Compose
reports it healthy while that file is less than 30 minutes old, so a slow price fetch
does not look like a crash. The migrate, scheduler and web containers have a read-only
root filesystem, no extra capabilities and a 512 MB memory cap. `/tmp` is writable, and
so is the scheduler state volume.

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
HTTPS email API with both HTML and plain text, plus the year-end CSV attachment.
The first recipient is in To; additional recipients are in Bcc. SMTP and SES use private envelope recipients.

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

Dry-run writes `report.html` and `report.txt`, plus CSV for year-end reports.
On POSIX, all preview files are created with mode `0600` (owner read/write only), and
existing preview files are tightened to that mode before overwriting. Windows uses its
ordinary file permissions. Keep the output directory private and real previews out of git.
The email uses inline styles and table layout, including a table-based value chart that
works without downloading images. Each market has the requested Sharesight-style columns
and subtotals, followed by the portfolio total. Missing price/FX data fails the entire
report with a non-zero exit instead of quietly omitting a holding. Stored stale data can
be used but is flagged. A failed send also exits non-zero and never pings monitoring.

For year-end delivery, use `--schedule year-end`. Its preview writes one
`report-portfolio-ID.html`, `.txt` and `.csv` per portfolio. It uses the same mail
settings, recipients and preview boundary as weekly/monthly delivery. The CSV is attached
for Gmail, SMTP, Postmark and SES. Explicit historical years remain available through
`year-end --portfolio NAME --year YYYY`; see the [summary guide](../README.md#nz-tax-year-summary).

When upgrading an existing server, the deploy action preserves its private schedule.
Add this entry to that file if it is missing, then restart the scheduler:

```toml
[[schedules]]
name = "year-end"
kind = "report"
frequency = "yearly"
month = 4
day = 1
at = "09:00"
period = "tax_year"
```

If migrating PR #29's SMTP settings, use `MAILER=smtp`, move `SMTP_SENDER` to
`MAIL_FROM` and `SMTP_RECIPIENT` to `MAIL_OWNER`. Retain the host, port and authentication
settings, then remove the two obsolete variables. The annual systemd units have been
removed; the deploy script disables an installed legacy year-end timer before restarting
the scheduler. On a manual upgrade, run the disable commands above.

Annual catch-up retains the due occurrence's tax year, even if the scheduler restarts
in a later year. Delivery continues after a failed portfolio and records each successful
portfolio in the scheduler JSON file, scoped to the schedule and original due time.
Retries skip recorded portfolios; these records are cleared when the occurrence advances.
Monitoring is pinged only after every portfolio succeeds. Manual reruns send again.
Check the provider after an uncertain send, since a crash before checkpointing or partial
recipient acceptance can still cause duplicate email. US closing data may not yet be stored at 09:00 NZDT
on 1 April; inspect carried-forward flags and refresh through 31 March before a manual
rerun if needed.

The email model uses the shared period engine in `returns.py` through `returns_loader`,
documented in [RETURNS.md](RETURNS.md). It combines all portfolios by listing in NZD.
The first email date is inclusive: its opening close is the preceding day. To compare
with the CLI, use that opening date in `--period OPENING:END`. Capital, net dividends,
currency gains and total returns use exactly the same FIFO results as the CLI.
Gross dividends, withholding tax and imputation credits are shown separately;
imputation is excluded from the default total return. Foreign portfolio broker FX
is translated to NZD using stored FX for the transaction date. Split and DRP handling
also comes from the shared engine. Record DRP reinvestments at their actual price.


## Private data retention

Keep Akahu snapshots for two years as the recommended default. They contain private
financial data in the database; review retention and remove older snapshots manually
when appropriate. No automatic snapshot deletion is configured.

Every Compose service rotates `json-file` container logs at 10 MB, retaining at most
three files per container. HTTP transport INFO logs are suppressed to avoid recording
holding symbols and request timestamps. Recreate containers to apply logging changes.
Keep database backups and any separately exported logs private as well.

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
service tries again every 5 minutes. Docker's health check only marks the container
unhealthy. It does not send a message.

To hear about a failure without signing in to the server, set `BACKUP_PUSH_URL` in `.env`
to a Uptime Kuma push URL and create a Push monitor with a 26-hour heartbeat. The URL is
a secret. After each dump is written, the service pings it with a short timeout and logs
only "monitoring ping sent" or "monitoring ping failed". It never prints the URL, and it
does not ping when the dump fails, so a missed heartbeat means dumps have stopped. A failed
ping does not by itself fail the dump.

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
8000 with one worker. `GET /healthz` returns `ok` and does not open a session or read the
database; Compose uses it as the web healthcheck. Only its host binding is published; the
database remains private.
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
The dry-run prints alerts currently due, and any rules skipped because units or a valuation
could not be checked. It neither sends notifications nor changes suppression state.
Its terminal output contains portfolio figures, so keep it private. Skipped rules are also
written to the log, with the rule name and the reason, and without prices or values.

Set these rules in the server's `schedules.toml`, then restart the scheduler:

```toml
[alerts]
enabled = true
daily_percent = "10"
portfolio_drop_percent = "5"
period_days = 7
stale_trading_days = 3
# Optional. Repeats portfolio-drop and stale rules while they stay triggered.
# Daily moves send once per pair of close dates and do not use this interval.
# cooldown_hours = 24

[alerts.holdings."NZX:EXAMPLE"]
daily_percent = "15"
# Other holding thresholds inherit the global values.
```

Threshold comparisons are strict: exactly 10% does not exceed a 10% rule. Daily
movement compares fresh closes on adjacent completed trading days in the holding's
listing currency, adjusted for splits. Fees, trades and currency movements do not
change this price comparison. The delivery row records the pair of close dates that
triggered it. That pair sends once: a second fetch the same day, or a weekend check
of Friday's closes, does not send again. A later qualifying pair sends even when the
earlier move never fell back under the threshold. Missing closes leave its
suppression state unchanged. A daily row recorded before close dates were stored has
no pair, so the next check can send the current move once and then records the dates.
Staleness counts completed exchange trading days, excluding weekends and exchange
holidays. If no fresh price exists, it counts from the first trade. Sold holdings do
not trigger holding alerts.

Portfolio value uses the shared returns engine's opening and closing valuations in
the portfolio's base currency across the configured calendar-day period. The period
ends on the most recent date completed by all portfolio markets and, when needed,
the FX market, so a close that is not yet due does not suppress the check. This is a
value-drop rule: sales and withdrawals can reduce it, purchases can offset falls,
and it is not a cash-flow-adjusted performance percentage. Incomplete or stale
valuations suppress that evaluation rather than inventing figures. A skipped rule is
logged and, on `alerts check --dry-run`, printed with the rule name and the reason.

Emails list the triggering figures and a reminder to open the private dashboard over
Tailscale. They reuse `MAILER`, sender and `recipients` (or `MAIL_OWNER` when empty).
Delivery state lives in the database and survives scheduler restarts. Each portfolio is
locked, evaluated and committed separately. Successful sends and rule clears commit
before moving to the next portfolio, so a later failure cannot roll them back.
Scheduled checks also checkpoint each completed portfolio in the scheduler JSON file
and skip it on retries of that occurrence. Dry runs neither commit nor checkpoint.
Each portfolio-drop and stale rule sends once until a successful evaluation clears
it. Optional `cooldown_hours` repeats those rules while they remain triggered. A
daily move ignores that cooldown and sends again only for a new pair of close dates.
Clearing a daily move, because it no longer exceeds the threshold or the holding is
sold, removes the recorded pair so a later qualifying pair can send.
Failed email delivery is retried without recording a sent alert; other portfolios
continue before the overall failure is reported. A process failure after email delivery but before the database
commit can cause a duplicate on retry, since email and the database cannot commit
atomically.

Push is off by default. In the server's private `.env`, set `ALERT_NTFY_URL` to an HTTPS
topic URL and optionally `ALERT_NTFY_TOKEN`, or set both `ALERT_PUSHOVER_TOKEN` and
`ALERT_PUSHOVER_USER`. ntfy takes precedence if both providers are configured. Recreate
the scheduler container after changing environment values. Push providers receive the
same portfolio figures as the email; use only a private topic or account. Never put
private URLs or tokens in TOML, commands or git. Legacy report monitoring in TOML
is supported only for migration; move it to `REPORT_KUMA_PUSH_URL` in `.env`. Push requests
refuse redirects to protect tokens and portfolio figures.
Push failures emit a generic warning without
secrets and do not retry an already successful email.
