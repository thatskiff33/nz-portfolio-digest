# Confirming holdings with Akahu

Akahu confirms units and supplies account balances. It does not supply a complete trade
history and never replaces or changes the ledger. Import your trades first. Balances are
saved for reference, not reconciled as cash because there is no cash ledger yet.

## Personal App setup

1. Sign in at [My Akahu](https://my.akahu.nz) and create a Personal App, following
   [Akahu's Personal App guide](https://developers.akahu.nz/docs/personal-apps).
2. Connect the investment provider you want to confirm, such as Sharesies. Grant account
   and investment holdings read access. This client only makes GET requests to
   `/v1/accounts` and `/v1/accounts/{id}/holdings`. It never requests a refresh or a payment.
3. Copy the app token and personal user token into `AKAHU_APP_TOKEN` and
   `AKAHU_USER_TOKEN` in your server's `.env`, with file permissions `600`.
   Do not paste tokens into shell commands, logs or issue reports.
4. Run `docker compose run --rm --build migrate` to apply the snapshot migration, then:

   ```sh
   docker compose run --rm migrate akahu sync --portfolio 'Personal'
   ```

For an installed CLI, run `portfolio-digest akahu sync --portfolio 'Personal'` from the
folder containing `.env`, with `DATABASE_URL` configured. `.env.example` lists the settings.

By default all `INVESTMENT` accounts are combined. If a token covers several portfolios,
set `AKAHU_ACCOUNT_IDS` to a JSON array of just this portfolio's account IDs in its own
runtime environment, for example `["acc_example"]`. Never confirm a partial account set
against a portfolio that includes other accounts. Account IDs can be found in My Akahu.
An inactive or missing configured account fails the run rather than confirming zero units.

Personal Apps generally refresh about once a day. Running sync more frequently reads the
same cached data. Snapshots retain Akahu's refresh metadata as well as the time of the run;
the command warns about cached data. A match confirms Akahu's cached view, not live holdings.
Inspect the refresh timestamps if an account appears stuck and reconnect it in My Akahu.

## Drift and manual mappings

Exit `0` means all mapped units match with no unknown holdings. Exit `1` means drift or an
operational failure. Each attempt for an existing portfolio creates an `akahu_snapshots`
row: `matched`, `drift` or `failed` (`running` if interrupted). Holdings and balances use
exact decimal strings in the snapshot JSON, not binary floats. The decoder rejects invalid
numbers and quantities with more than 24 decimal places instead of silently rounding them.

The report prints instrument IDs, ledger units, observed units and the difference
(observed minus ledger). It detects both missing and extra units, including instruments
present on just one side. `report` also records unmapped holdings; `observations` stores
selected account balances, refresh timestamps and holding identifiers. These are private
financial data in your database. Keep backups private and never commit snapshots to git.

Codes match case-insensitively with exchange when provided. Without an exchange, a code
must identify exactly one instrument. Unknown or ambiguous holdings cause drift. To resolve
one, inspect the snapshot report locally, create the instrument if necessary, then insert
an explicit override in the database using your own values:

```sql
INSERT INTO akahu_mappings (portfolio_id, account_id, holding_id, instrument_id)
VALUES (1, 'acc_example', 'holding_example', 42);
```

Overrides are scoped to portfolio and account and take precedence over code matching.
Use the holding's `_id` from Akahu. Never guess an exchange or map an unknown holding to
something similar just to clear an alert.

For unit drift, compare the provider's statement with imports, sales, corporate actions,
transfers, opening balances and dividend reinvestments. Correct or import the missing
ledger records with supporting evidence. The shared reconciliation function counts buys,
opening balances, signed adjustments and explicit DRP units, and subtracts sells. Record
a DRP once, either as explicit income units or a trade, to avoid double counting. Wait for
Akahu's next refresh after recent trades, then rerun. Never overwrite trades from Akahu.

## Nightly run and monitoring

This checkout has no application scheduler or price-fetch job yet. Add this to the server
user's crontab alongside the price-fetch entry when it is available. Replace the project
path and portfolio name. Cron uses the server's timezone; choose the equivalent of 03:15
Pacific/Auckland if your server uses UTC. Keep logs private.

```cron
15 3 * * * cd /srv/portfolio-digest && docker compose run --rm migrate akahu sync --portfolio 'Personal'
```

The Compose command reads credentials from the project's `.env`. Build the image and run
migrations after deploying changes. Use your scheduler's non-zero exit alerts for drift.
Optionally create a Push monitor in Uptime Kuma with a heartbeat interval longer than a day
(e.g. 26 hours) and set `UPTIME_KUMA_PUSH_URL` to its full push URL in `.env`. A successful
committed match sends `status=up`. Drift and fetch failures send no success ping; a missed
heartbeat alerts. A failed push exits non-zero while retaining the committed snapshot.
429 responses retry up to five attempts with exponential backoff and bounded Retry-After;
other HTTP failures fail immediately. No live network calls are made by the tests.
