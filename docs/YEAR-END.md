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
The recipients in `schedules.toml` override `MAIL_OWNER` for every report. Use `--config`
to select another schedule file. See [email setup](DEPLOY.md#email-reports).
Preview mode needs no mail configuration.

The summary includes per-holding and portfolio realised gains and losses, gross dividends,
imputation credits, resident and foreign withholding, net received or reinvested, opening
and closing 31 March values, and total return. CSV retains full calculation precision;
HTML displays amounts to two decimal places. Realised sales use **historical FIFO cost**,
including acquisition fees and net of sale fees. Annual performance uses the existing
returns engine, which starts from opening market value and includes gross dividends.
Its percentage denominator is opening value plus purchase outlays. Splits preserve cost;
DRP units need a recorded buy or quantity adjustment as well as the income record.
Opening balances and transfers use their recorded value, so supply full acquisition
history for reliable historical cost. Missing prices or FX stop the report; carried-forward
data is disclosed in both outputs. This command never downloads prices or changes history.

The foreign cost figure is the peak historical NZD acquisition cost during the year,
including the opening position, rather than market value or just year-end cost. Same-day
additions precede sales because trade records have no execution times. All non-NZX holdings,
including Australian shares, are conservatively included. A warning appears **above
NZ$50,000**. FIF exemptions and calculations are outside this report; the threshold applies
per person across portfolios, so this portfolio alone cannot establish an exemption.

The current income schema has one combined withholding field. The report classifies it as
resident withholding for NZX holdings and foreign withholding otherwise. Check this
assumption against tax statements, particularly dual listings and NZ funds investing
overseas. Gross cash dividends exclude imputation credits, which are shown separately.
NZ generally does not tax capital gains for long-term investors, but exceptions apply.
This summary is information, not tax advice.

The shared scheduler runs the `year-end` entry in `schedules.toml` on
**1 April at 09:00 Pacific/Auckland**, sending one email per NZD portfolio for the latest
year ended 31 March. No separate annual systemd timer is needed. If one was installed
as the standalone year-end timer used before November 2026, disable it before starting the shared scheduler; see
[deployment](DEPLOY.md#shared-price-and-report-schedule).

The 09:00 send uses data already stored. The US 31 March close coincides with this time
in NZ daylight saving, before the normal daily price fetch, so US holdings may use the
preceding stored close. Review carried-forward flags; refresh prices and FX through
31 March and rerun manually if you need that closing observation.

For an all-portfolio preview, use `year-end --all --year 2026 --dry-run --out
./private-preview`; each portfolio gets a numbered subdirectory. Other portfolios continue
if one fails, and the command exits non-zero if any failed. Manual reruns send again.
The scheduler retries failed occurrences after restart using their original tax year;
a retry may resend to portfolios already delivered. Check provider delivery before
retrying an uncertain result. Inspect failures with `docker compose logs scheduler`.

The scheduled report can also be sent or previewed through the shared report command:

```sh
portfolio-digest report send-now --schedule year-end
portfolio-digest report send-now --schedule year-end --dry-run --out annual.html
```

The latter writes `annual-portfolio-ID.html`, `.txt` and `.csv` for each portfolio,
using its database ID. A scheduled report stops on the first failed portfolio, exits
non-zero and does not ping monitoring. First startup schedules future occurrences;
existing pending occurrences catch up once after downtime.

