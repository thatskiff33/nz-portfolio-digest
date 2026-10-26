# Importing and reconciling holdings

Start with the full trade history from each broker. This preserves the real cost basis.
Do not create one opening balance per current holding as a shortcut. Manual opening
balances remain available for genuine cases where no trade history exists, such as an
inherited holding.

Every attempt is recorded as an import run. A malformed file imports nothing. Importing
an overlapping or identical export is safe because each broker row has a stable reference.
Use `portfolio-digest import list` to see the audit trail and `portfolio-digest import undo
ID` to remove the rows written by one successful run. Undo does not remove instruments or
touch trades from another run. If another successful run supplied the same trade, undo
retains it and reassigns it to that run. Undo those runs too to remove their shared trades.
Identical repeated references within one input collapse to one trade. A stored reference
is only a duplicate when its trade fields match. Conflicting references fail file imports.
For Gmail, a conflicting message is skipped with its message ID and reference in the CLI
output, allowing the rest of the label to import. The stored trade is retained; review the
amendment and correct the history explicitly rather than overwriting it automatically.
The first accepted version of a new reference in a Gmail batch is retained.
Currency prefixes on prices and fees must agree with their declared currency.

Broker exports contain private financial information. Keep them outside the repository,
prefer the ignored `imports/` directory on the server, and remove them when they are no
longer needed.

## Sharesies

In Sharesies, choose **Account > Generate Reports > Transaction Report**. Import the CSV:

```sh
portfolio-digest import sharesies imports/transactions.csv --portfolio "Personal"
```

The importer handles NZX, ASX and US trades, including Smart ETFs and fractional units.
It imports buys and sells and reports non-trade rows as skipped. Share transfers are also
skipped because their original trades should come from the other broker's history.
Sharesies exchange rates are based on NZD, so these reports can only be imported into an
NZD-base portfolio.

## ASB Securities

ASB Securities has no share-trade CSV. Use the historical statement once, then use the
Gmail feed for new contract notes.

For the historical statement, extract layout-preserving text from the PDF on a trusted
machine, then import the text. The PDF itself is deliberately refused.

```sh
pdftotext -layout trade-statement.pdf imports/asb-history.txt
portfolio-digest import asb-statement imports/asb-history.txt --portfolio "Personal"
```

Statement fees include brokerage plus GST when a separate GST column appears between
Brokerage and Consideration. Without that column, Brokerage must contain the total fee
including any GST, as email fees include both amounts. No tax amount is inferred.
Contract numbers must be 1 to 119 digits.

Each unreadable row is named by line number and the whole run fails, so a damaged row can
never silently leave the holding short. A saved contract-note email can also be tested or
imported directly:

```sh
portfolio-digest import asb-email imports/contract-note.eml --portfolio "Personal"
```

For the ongoing feed:

1. In Google Cloud, create an OAuth client of type **Desktop app** and enable the Gmail API.
2. In Gmail, create a dedicated label and apply it only to ASB contract-note emails.
3. Put `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and `GMAIL_LABEL` in the private `.env`.
4. On a machine with a browser, run `portfolio-digest gmail authorise`. It requests only
   `gmail.readonly` and writes the refresh token to `.env.gmail` with owner-only permissions.
   Existing files are refused unless `--force` is supplied. Symlinks are always refused.
   The command names the variable to copy; it never prints the token.
5. Copy `GMAIL_REFRESH_TOKEN` from that file into the server's private `.env`, then securely
   remove the temporary file.
6. Run the feed whenever required:

```sh
portfolio-digest import asb-gmail --portfolio "Personal"
```

Only messages carrying the configured label are read. OAuth secrets and tokens are never
shown in errors or stored in import runs.

## Sharesight-format CSV

Some brokers export a CSV described as "for Sharesight". This is only a file format. The
app does not connect to, scrape, or call Sharesight.

```sh
portfolio-digest import sharesight-csv imports/broker-trades.csv --portfolio "Personal"
```

Recognised columns are `Trade Date`, `Instrument Code`, `Market Code`, `Quantity`, `Price
in Dollars`, `Transaction Type`, `Exchange Rate`, `Brokerage`, `Brokerage Currency` and
`Comments`. The importer accepts BUY, SELL and OPENING_BALANCE. Enter corporate actions as
manual adjustments so their signed unit effect is explicit.

## Manual entries

Use manual entries for corrections, gifts and the rare real opening balance:

```sh
portfolio-digest trade add --portfolio "Personal" --date 2024-06-30 \
  --type adjustment --exchange NZX --code AIR --quantity 10 --price 0 \
  --notes "Made-up example: gifted shares"
```

Types are `buy`, `sell`, `opening_balance` and `adjustment`. Buy, sell and opening-balance
quantities must be positive. Adjustments may be positive or negative. Manual fees cannot
be negative.

## Reconciliation

Export or prepare a current broker or registry snapshot as UTF-8 CSV with these columns:

```csv
Exchange,Code,Units
NZX,AIR,10.34567890
ASX,BHP,2.5
NASDAQ,MSFT,0.125
```

Then compare it with the units calculated from all stored trades:

```sh
portfolio-digest reconcile sharesies imports/current-holdings.csv --portfolio "Personal"
```

The command exits successfully only when every exact unit total matches. Its mismatch
table includes holdings present on only one side as well as differing balances. The source
argument is a report label, so registry names such as `computershare` are also suitable.
