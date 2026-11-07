# NZ tax-year summary

`--year 2026` means 1 April 2025 to 31 March 2026. Run a preview first:

```sh
portfolio-digest year-end --portfolio "Example" --year 2026 --dry-run --out ./private-preview
portfolio-digest year-end --portfolio "Example" --year 2026
```

The preview writes `year-end-2026.html`, `year-end-2026.txt` and
`year-end-2026.csv` and sends nothing. Keep these private financial reports outside git.
The send command emails HTML and plain text with a CSV attachment, using the same
`MAILER`, `MAIL_FROM`, `MAIL_OWNER` and provider credentials as weekly and monthly reports.
Non-empty `REPORT_RECIPIENTS` in the server `.env` overrides TOML recipients for every
report. TOML recipients then take precedence over the `MAIL_OWNER` fallback. Keep private
recipients and `REPORT_KUMA_PUSH_URL` in `.env`; the latter overrides legacy TOML monitoring.
Empty or whitespace-only report environment settings leave TOML values in effect. Use `--config`
to select another schedule file. See [email setup](DEPLOY.md#email-reports).
Preview mode needs no mail configuration.

The summary includes per-holding and portfolio realised gains and losses, gross dividends,
imputation credits, resident and foreign withholding, net received or reinvested, opening
and closing 31 March values, and total return. CSV retains full calculation precision;
HTML displays amounts to two decimal places. Realised sales use **historical FIFO cost**,
including acquisition fees and net of sale fees. Annual performance uses the existing
returns engine, which starts from opening market value and includes net cash dividends.
Its percentage denominator is opening value plus purchase outlays plus non-cash
value moved in (transfers in, gifts, opening balances). Registry-recorded
splits and consolidations need no extra quantity adjustment. A Yahoo-only split needs
a matching quantity adjustment. Imported DRPs that include a DRP quantity create the
linked buy. Add a purchase manually only for residual cash or a partial reinvestment.
Opening balances and transfers use their recorded value, so supply full acquisition
history for reliable historical cost. Missing prices or FX stop the report; carried-forward
data is disclosed in both outputs. This command never downloads prices or changes history.

The foreign cost figure is the peak historical NZD acquisition cost during the year,
including the opening position, rather than market value or just year-end cost. Same-day
additions precede sales because trade records have no execution times. All non-NZX holdings,
including Australian shares, are conservatively included. A warning appears **above
NZ$50,000**. FIF exemptions and calculations are outside this report; the threshold applies
per person across portfolios, so this portfolio alone cannot establish an exemption.

Resident and foreign withholding follow the components stored from broker or registry
evidence. Any unclassified remainder is resident withholding for an NZX holding and
foreign withholding otherwise. The report names each holding where that remainder
was used. Check this classification against your tax statements. Gross cash dividends
exclude imputation credits, which are shown separately.
NZ generally does not tax capital gains for long-term investors, but exceptions apply.
This summary is information, not tax advice. Tax points are to confirm with a tax specialist.

The shared scheduler runs the `year-end` entry in `schedules.toml` on
**1 April at 19:00 Pacific/Auckland**, sending one email per NZD portfolio for the latest
year ended 31 March. No separate annual systemd timer is needed. If one was installed
as the standalone year-end timer used before November 2026, disable it before starting the shared scheduler; see
[deployment](DEPLOY.md#shared-price-and-report-schedule).

The 19:00 send follows the 18:30 price and FX fetch, allowing the US 31 March close
and FX fix to be stored before delivery. Source failures can still leave observations
stale. Review carried-forward flags; refresh prices and FX through 31 March and rerun
manually after a source failure.

For an all-portfolio preview, use `year-end --all --year 2026 --dry-run --out
./private-preview`; each portfolio gets a numbered subdirectory. Other portfolios continue
if one fails, and the command exits non-zero if any failed. Manual reruns send again.
The scheduler retries each failed job independently after 30 minutes, doubling the delay
to a maximum of 6 hours. Retry times and the original tax year survive restarts in the
existing scheduler state volume. A partial price fetch advances with warnings; if every
instrument fails, that price job retries while other schedules continue. No holdings
counts as success. Alerts still check the committed data after partial fetches.
Inspect failures with `docker compose logs scheduler`.

The scheduled report can also be sent or previewed through the shared report command:

```sh
portfolio-digest report send-now --schedule year-end
mkdir -p -m 700 ./private-preview
portfolio-digest report send-now --schedule year-end --dry-run --out ./private-preview/annual.html
```

The latter writes `./private-preview/annual-portfolio-ID.html`, `.txt` and `.csv` for each portfolio,
using its database ID. Scheduled year-end reports continue past failed portfolios and
record each successful portfolio in the scheduler state file. Retries skip those
portfolios and monitoring is pinged only when the whole occurrence succeeds. Alerts
commit sent state per portfolio and use the same occurrence checkpoints on scheduled
checks. Keep the state volume and run one scheduler replica.

Delivery remains at least once: provider acceptance followed by a crash before the
checkpoint or database commit can duplicate email. Partial recipient acceptance is
also uncertain. Check provider delivery before retrying an uncertain result manually.
Only configuration or scheduler-state errors stop the scheduler with a non-zero exit.
First startup schedules future occurrences; existing pending occurrences catch up once
after downtime, with any intervening skipped occurrences logged.
