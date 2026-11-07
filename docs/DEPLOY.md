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

The first backfill starts seven calendar days before each instrument's first trade.
To deliberately use a later date, add `--start YYYY-MM-DD`. Normal runs re-read recent
history so corrections from the source replace carried-forward values. Commands are safe to repeat: an existing day is
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
report periods and recipients. Prices run at 11:15 and 18:30 Auckland time. Weekly reports
run Sunday at 20:00 for the preceding seven completed calendar days. Monthly reports run
on the 1st at 19:00 for the previous calendar month. Year-end summaries run on 1 April
at 19:00 for the NZ tax year just ended, one email per NZD portfolio. Weekly and monthly
reports exclude today's unfinished markets.
All schedules use `Pacific/Auckland`, including daylight saving. The 11:15 morning fetch
follows both the US price close and the 17:00 New York FX close in every daylight-saving
combination. The evening fetch refreshes NZX and ASX prices.
Monthly and year-end delivery at 19:00 follows that 18:30 fetch. The startup timing
warning checks schedule order only, not whether the fetch succeeds or finishes.

Existing servers preserve their private `schedules.toml` during deployment. Edit the
`prices-morning` entry there from `at = "10:30"` to `at = "11:15"`. Keep the monthly
and year-end entries at `at = "19:00"`, then restart the scheduler with
`docker compose restart scheduler`. The `at` time is part of the schedule identity,
so restarting after this change can trigger an extra price fetch on the same day.
Price fetches are safe to repeat: existing price and FX rows are updated by their
unique day keys, without duplicates; carried-forward values cannot replace fresh data.

If you previously installed the price systemd timer or the standalone year-end timer used before November 2026, disable
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
Monitoring is pinged only after successful delivery. Monitoring URLs must use HTTPS unless
they point at `localhost`, a loopback address or a Tailscale address (100.64.0.0/10).
Redirects count as a failed ping and are never followed. Ping failures are logged without
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
Retry times, failure counts, successful portfolio deliveries and Postmark recipient
checkpoints are saved in the existing
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
state or committing the database can cause a duplicate. Postmark attempts every recipient
and reports only the number of failures. Scheduled retries skip recipients already accepted
for that report or portfolio, including after a restart or alert transaction rollback.
Recipient checkpoints store hashes protected by a random per-install HMAC key, with no
addresses or message contents, and expire when the occurrence completes. The scheduler
state file and its companion `<state-file>.key` file use mode `0600` (owner read/write
only). Keep the key separate from copies of the state file shared for troubleshooting:
a reader with both files can test candidate addresses. Retain the key in private backups
and restore it alongside the state file. A missing or invalid key stops the scheduler;
restore the original key before restarting. Existing unkeyed checkpoints are converted
on startup without losing delivery progress. Manual sends do not use scheduler checkpoints.
SMTP partial refusals can still leave some recipients with the email. Check the provider
before a manual retry. Keep the state volume and run only one scheduler replica.
Inspect it with `docker compose logs scheduler`.
The scheduler writes `/tmp/scheduler-heartbeat` every time around its loop. Compose
reports it healthy while that file is less than 30 minutes old, so a slow price fetch
does not look like a crash. The migrate, scheduler and web containers have a read-only
root filesystem, no extra capabilities and a 512 MB memory cap. `/tmp` is writable, and
so is the scheduler state volume.

### Systemd user timers

The Akahu sync uses a systemd user timer, separate from the shared Compose price and
report scheduler. Enable lingering so user timers keep running while you are logged out:

```sh
loginctl enable-linger $USER
```

Install only the Akahu units from `deploy/systemd/`; see [Akahu setup](AKAHU.md#nightly-run-and-monitoring).
Archived price units in `deploy/legacy/` are historical reference only. Keep them disabled.

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
`year-end --portfolio NAME --year YYYY`; see the [summary guide](YEAR-END.md).

When upgrading an existing server, the deploy action preserves its private schedule.
Add this entry to that file if it is missing, then restart the scheduler:

```toml
[[schedules]]
name = "year-end"
kind = "report"
frequency = "yearly"
month = 4
day = 1
at = "19:00"
period = "tax_year"
```

If migrating SMTP settings from the standalone year-end timer used before November 2026, use `MAILER=smtp`, move `SMTP_SENDER` to
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
recipient acceptance can still cause duplicate email. The 19:00 delivery follows the
18:30 fetch, but source failures can still leave stale observations. Inspect
carried-forward flags and refresh through 31 March before a manual rerun if needed.

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

Keep the pre-update dump and its release metadata together. First record the current
commit and database revision, then take a fresh backup before updating code. Run each
step only if the previous one succeeded. On migration or health failure, leave scheduler
and web stopped and use the rollback procedure below.

```sh
cd ~/portfolio-digest
previous_release=$(git rev-parse HEAD)
previous_revision=$(docker compose exec -T db psql -U portfolio -d portfolio -Atc \
  'SELECT version_num FROM alembic_version')
docker compose run --rm backup --once            # stop unless it prints "backup: wrote"
# Write private metadata beside the new dump, including its filename from that output.
# Substitute the actual dump filename and BACKUP_DIR if configured differently.
(
  umask 077
  printf '%s\n%s\n' "$previous_release" "$previous_revision" > backups/portfolio-YYYYMMDDTHHMMSSZ.release.txt
)
```

Detect dashboard use before stopping anything. An existing container, even when stopped,
or a non-empty password in the server's `.env` enables it. Read the file as data, never
source it or print its contents. This is the same detection used by `scripts/deploy.sh`:

```sh
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
```

Preserve the private `schedules.toml` before a fast-forward update, as the deploy script
does. Restore the private copy afterwards and add any newly documented schedule entries.
Keep this copy outside git with owner-only access.

```sh
# After preserving schedules.toml:
git restore schedules.toml
git pull --ff-only origin main
# Restore the private schedules.toml now.
# The scheduler runs as uid 10001 and must be able to read this file.
chmod 0644 schedules.toml
set -- migrate scheduler
if [ "$web_enabled" = true ]; then set -- "$@" web; fi
docker compose build "$@"
docker compose stop scheduler web
docker compose run --rm migrate
docker compose run --rm migrate health
# Only after both commands succeeded:
set -- db backup scheduler
if [ "$web_enabled" = true ]; then set -- "$@" web; fi
docker compose up -d --no-deps "$@"
```

Disable any legacy year-end timer using the commands under
[the shared schedule](#shared-price-and-report-schedule) before migration. Also stop the
Akahu timer during maintenance if installed; restart it only after health passes.

### Failed-migration rollback

A code checkout alone cannot roll back a database schema. Keep scheduler, web, backup
and any Akahu timer stopped. Use the recorded previous commit and the pre-update dump.
Do not use `git pull` during rollback or recovery: it selects newer code that may not
match the saved schema. Preserve the private schedule around each checkout and restore
its mode to `0644` so the scheduler can read it.

If `migrate` itself failed, its database transaction rolled back: check out and rebuild
the previous release, skip the schema reset and dump restore below, then run health
before restarting services. A restore is needed when migration committed successfully
but health or a later check failed and you must return to the pre-update schema.

For a restore, reset `public` first. Newer migrations can add foreign keys from tables
absent in the dump, which prevents `pg_restore --clean --if-exists` from dropping the
older tables. The reset removes all current objects in `public`, then restores ownership
and grants for `portfolio`. Only do this with a verified pre-update dump available.

```sh
docker compose stop scheduler web backup
git checkout "$previous_release"
set -- migrate scheduler
if [ "$web_enabled" = true ]; then set -- "$@" web; fi
docker compose build "$@"
docker compose exec -T db psql -U portfolio -d portfolio -v ON_ERROR_STOP=1 --single-transaction <<'SQL'
DROP SCHEMA public CASCADE;
CREATE SCHEMA public AUTHORIZATION portfolio;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO portfolio;
SQL
docker compose exec -T db pg_restore -U portfolio --single-transaction \
  -d portfolio < backups/portfolio-YYYYMMDDTHHMMSSZ.dump
docker compose run --rm migrate health
# Only after restore and health succeed, without running migrate:
set -- db backup scheduler
if [ "$web_enabled" = true ]; then set -- "$@" web; fi
docker compose up -d --no-deps "$@"
```

Use the metadata file to recover `previous_release` in a new shell. The schema reset
and restore are separate transactions. If restore fails after the reset, `public` stays
empty; leave writers stopped, investigate and repeat the restore. Do not restart services
against an empty or uncertain schema.

## Backups

The `backup` service dumps the database when it starts and every 24 hours after that, into
`BACKUP_DIR` (default `./backups`). The files belong to `BACKUP_UID:BACKUP_GID` and only
that user can read them.

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

```sh
docker compose ps backup        # "healthy" means a dump was written in the last 26 hours
docker compose logs backup      # one line per dump, and the reason for any failure
```

### Encrypted off-site copies

Copy dumps and their release metadata to a separate location off the server. Encrypt them
before transfer, for example with `age` or `gpg`. Keep the decryption key separately from
both the server and the backup destination, with a recoverable private copy. For example,
using an invented recipient placeholder:

```sh
age -r '<backup recipient public key>' -o portfolio.dump.age backups/portfolio-YYYYMMDDTHHMMSSZ.dump
```

Transfer the encrypted file and encrypted metadata through your normal secure copy
process. Never put passwords or private keys in commands or git. Periodically retrieve,
decrypt and test a copy using the scratch procedure below, including access to the
separately stored key. Retention on the server is not off-site retention.

### Test a restore

Use a disposable database whose name contains `restoretest`. This does not touch the
production database. The administrator creates it for the unprivileged `portfolio` role:

```sh
docker compose exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
CREATE DATABASE portfolio_restoretest OWNER portfolio;
REVOKE ALL ON DATABASE portfolio_restoretest FROM PUBLIC;
SQL
docker compose exec -T db pg_restore -U portfolio --clean --if-exists --single-transaction \
  -d portfolio_restoretest < backups/portfolio-YYYYMMDDTHHMMSSZ.dump
docker compose exec -T db psql -U portfolio -d portfolio_restoretest -Atc \
  'SELECT version_num FROM alembic_version'
```

Select the release recorded with the dump and verify that its Alembic head matches the
restored revision. If metadata is missing, use the isolated restore's `alembic_version`
and release history to identify a compatible release; never infer it from the filename.
Use a separate maintenance window for checking out that release, with scheduler, web,
backup and any Akahu timer stopped and the private schedule preserved. Rebuild `migrate`,
then run health against only the scratch database. This override reuses the private
container credentials without putting a password in arguments or output:

```sh
git checkout <matching-release-commit>
docker compose build migrate
docker compose run --rm --entrypoint python migrate -c '
import os
from sqlalchemy.engine import make_url
from portfolio_digest.cli import main
url = make_url(os.environ["DATABASE_URL"]).set(database="portfolio_restoretest")
os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
raise SystemExit(main(["health"]))
'
docker compose exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP DATABASE portfolio_restoretest'
```

Do not migrate the scratch database before health: that would conceal a release mismatch.
Return to the original release and rebuild its images before resuming services. If any
step fails, inspect it and remove the scratch database once finished. Leave production
writers stopped until their own release and health are verified.

## Restoring

First detect optional dashboard use with the snippet in [Updating](#updating). Stop
scheduler, web and backup, and any installed Akahu timer, before restoring. Check out
and rebuild the release matching the dump's Alembic revision, preserving the private
schedule. Do not use `git pull`. For dumps without release metadata, first use
[Test a restore](#test-a-restore) to identify a matching release.

Reset `public` before restoring, using the same ownership and grants as first install.
This removes newer objects and foreign keys that are absent from the dump. Have a verified
dump available before running this destructive reset. The reset commits separately:
`--single-transaction` rolls back a failed restore, but leaves the schema empty after
the reset. Leave writers stopped if either step fails. Restore as
`portfolio`, which owns the dump's objects, so the dump cannot run with superuser rights.
Run each command only after the preceding one succeeds. `--no-deps` prevents Compose
from starting the one-off migration service again after health has passed:

```sh
docker compose stop scheduler web backup
git checkout <matching-release-commit>
set -- migrate scheduler
if [ "$web_enabled" = true ]; then set -- "$@" web; fi
docker compose build "$@"
docker compose up -d --wait db
docker compose exec -T db psql -U portfolio -d portfolio -v ON_ERROR_STOP=1 --single-transaction <<'SQL'
DROP SCHEMA public CASCADE;
CREATE SCHEMA public AUTHORIZATION portfolio;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO portfolio;
SQL
docker compose exec -T db pg_restore -U portfolio --single-transaction \
  -d portfolio < backups/portfolio-YYYYMMDDTHHMMSSZ.dump
docker compose run --rm migrate health
# Only after health succeeds; do not migrate the restored schema forward:
set -- db backup scheduler
if [ "$web_enabled" = true ]; then set -- "$@" web; fi
docker compose up -d --no-deps "$@"
```

On a new server, follow [First install](#first-install) only through creating `.env`
and the backup folder. Copy the private schedule and dump securely. Set the dashboard
password only if needed, then use the same detection, matching-release checkout, build,
database start, restore, health and service start sequence above. Do not create a new
schema with `migrate` before or after restoring. Restart any Akahu timer only after health
passes. Leave services stopped on restore or health failure.

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

If your existing dashboard uses Tailscale Serve, set `DASHBOARD_HOSTNAMES` in the
server's private `.env` to its dashboard hostname before redeploying `web`, for
example `DASHBOARD_HOSTNAMES=dashboard.example.ts.net` (a placeholder, without a
scheme or port). Otherwise, the Host allow-list rejects requests with HTTP 400.

## Private dashboard over Tailscale

The `web` service shares the app package and database. It runs FastAPI on container port
8000 with one worker. `GET /healthz` requires sign-in and returns `ok` without reading the
database. Compose checks its anonymous 303 sign-in redirect for liveness. Only its
host binding is published; the database remains private.
The default is `127.0.0.1:8000`. Never publish it on a wildcard or public address, configure
router forwarding, or use Tailscale Funnel.

1. Install Tailscale on the server and the devices you use to view the dashboard. Restrict
   access to trusted devices using your tailnet's access policy. Every page also requires
   the dashboard password before showing financial data, including import notes.
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
restarting web. Set `DASHBOARD_HOSTNAMES` to the hostname in the tailnet HTTPS URL
printed by Tailscale Serve, without a scheme or port. It accepts comma-separated
hostnames. The Host allow-list also accepts `DASHBOARD_BIND`, `127.0.0.1` and
`localhost`, with or without a port. For direct tailnet
HTTP leave `DASHBOARD_SECURE_COOKIE=false`; the transport is encrypted by Tailscale.
No reverse proxy trust is enabled, and no external CDNs, fonts or analytics are loaded.

Every page requires sign-in except the sign-in form and static assets. Sign-out is a
CSRF-protected POST. Anonymous GETs redirect to sign-in and return to the requested
local path and query after successful sign-in. The sign-in page shows no portfolio data.
Every POST checks a CSRF token, including sign-in and sign-out. Sessions are server-side,
expire eight hours after sign-in or after 30 minutes without activity, and use HttpOnly,
SameSite=Strict cookies. HTTPS uses the `__Host-portfolio_session` cookie name;
`DASHBOARD_SECURE_COOKIE` accepts boolean values such as `true`, `1` and `yes`.
Opening the sign-in form creates a CSRF session lasting ten minutes. Anonymous sessions
have a separate 1,024-session cap and evict the oldest anonymous session when full;
they never evict signed-in sessions. Signed-in sessions have their own 1,024-session cap.
Five failed sign-ins in five minutes block that client address for the remainder of
that window. Behind Tailscale Serve or a shared Docker proxy, the per-address limit
can act globally because clients share the apparent address. Forwarded headers are
not trusted. A global ceiling of 50 failures in five minutes also limits attempts
across all clients. Restarting web revokes sessions and resets these limits.
Do not run multiple workers or replicas.
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
backup and records recovery metadata before updating code. It builds the app and optional
web, stops scheduler and web before migrating, checks database health, then starts the
services including web when enabled. Migration or health failure leaves scheduler and web
stopped for investigation.
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
the scheduler container after changing environment values. By default
(`ALERT_PUSH_DETAIL=minimal`) a push says only how many new alerts there are and points
to your email or the dashboard, so no portfolio names, holdings or figures reach the push
provider. Set `ALERT_PUSH_DETAIL=full` to push the same text as the email instead; then use
only a private topic or account. Never put
private URLs or tokens in TOML, commands or git. Legacy report monitoring in TOML
is supported only for migration; move it to `REPORT_KUMA_PUSH_URL` in `.env`. Push requests
refuse redirects to protect tokens and portfolio figures.
Push failures emit a generic warning without
secrets and do not retry an already successful email.
