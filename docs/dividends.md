# Dividends and corporate actions

Broker and registry records are the source of truth. All examples and test fixtures are
fictional. Run `portfolio-digest migrate` before using these commands.

## Broker imports

`portfolio-digest import sharesies imports/transactions.csv --portfolio Personal` reads
`DIVIDEND` and `DISTRIBUTION` rows as income. Alongside the existing date, market, code
and currency columns, income rows require `Gross amount` and `Net amount` (or `Amount`).
Supported tax columns are `Withholding tax`, `RWT` (or `Resident withholding tax`),
`Foreign withholding tax`, `Imputation credits` and `Franking credits`. Optional columns
are `Ex date` and `DRP quantity`. Quantity and price cells may be blank for income rows.
When the report omits gross or tax details, obtain the dividend advice rather than
inferring tax from the net cash. An incomplete income row fails the import with a line
number. A missing detail must be supplied from broker or registry evidence.

Gross means the cash dividend before withholding, excluding imputation and franking.
Gross must equal net plus withholding. A total withholding column, when supplied with
tax components, must agree with their sum. Blank tax components mean zero; an unclassified
total withholding is retained without inventing a tax classification. US treaty withholding
is commonly 15%, but the actual statement amount always wins. AU franking is recorded
separately and is never included in NZ imputation gross-up or return totals.

Import a saved ASB dividend email or text extracted from a dividend advice:

```sh
pdftotext -layout dividend.pdf imports/dividend.txt
portfolio-digest import asb-dividend imports/dividend.txt --portfolio Personal
```

The parser accepts one advice per file, with these case-insensitive `Label: value` fields
(as plain text, or plain/HTML MIME email):

```text
Statement reference: FICTIONAL-100
Security code: EXM
Market: NZX
Currency: NZD
Payment date: 20/06/2024
Ex date: 01/06/2024
Gross dividend: NZD 100.00
Net dividend: NZD 90.00
Resident withholding tax: NZD 10.00
Imputation credits: NZD 28.00
DRP quantity: 3
```

Optional fields include `Withholding tax`, `Foreign withholding tax`, `Franking credits`,
`Income kind: distribution`, `Ex date` and `DRP quantity`. Unknown layouts are refused;
check extracted text against the original advice before importing. The ASB Gmail
contract-note feed remains for trades; saved dividend emails use `asb-dividend`.

A DRP creates both income and a linked buy on payment date. The net amount is treated as
fully reinvested. The trade retains the exact consideration, even when its unit price
needs rounding to six decimal places. If the advice includes residual cash or a partial
reinvestment, do not enter the full net amount as a DRP; record the actual purchase
separately. Imports, duplicates and undo cover both income and its DRP trade. Independently
reimported evidence retains the income when an earlier run is undone. Broker references
must be stable; use the same source and reference for overlapping files.

## Missing-income suggestions

```sh
portfolio-digest dividends suggest --portfolio Personal --instrument NZX:EXM \
  --start 2024-01-01 --end 2024-12-31
portfolio-digest dividends confirm 12 --evidence registry --reference FICTIONAL-100 \
  --paid-date 2024-06-20 --gross 100 --net 90 --rwt 10 --imputation 28
portfolio-digest dividends reject 13
```

Yahoo's dividend history is multiplied by units held immediately before ex-date trades,
including splits effective on that date. These are estimates: Yahoo can revise historical
per-unit amounts and its ex date is not a payment date. Estimates stay in
`dividend_suggestions`, outside income and returns. `suggest` prints IDs and statuses;
repeat runs retain rejected and confirmed decisions. It suppresses new suggestions when
income with that ex date is already recorded. An advice without an ex date may still
produce a suggestion, so check the broker record before confirming it.

Confirmation requires actual gross, net, payment date and a broker/registry reference.
Optional flags are `--tax`, `--rwt`, `--foreign-tax`, `--imputation`, `--franking`, and `--drp`.
It links matching existing income instead of double counting it and refuses ambiguous or
conflicting amounts. Rejection never creates income. Already decided suggestions cannot
be decided again. Fetching needs network access; importing, recording and reporting do not.

## Corporate actions

Split and consolidation ratios are old units (`before`) to new units (`after`). They
apply to all portfolios before trades on the effective date, retain total cost base, and
are recognised by the price quality check. Do not also enter an adjustment trade for
the same split. Existing histories that already contain split adjustments must be
reconciled before recording that split here. Registry records survive Yahoo refreshes;
a conflicting Yahoo ratio stops that price fetch with an error.

```sh
portfolio-digest corporate-actions split NZX:EXM --date 2024-06-01 \
  --reference FICTIONAL-SPLIT --details '{"before":"1","after":"2"}'
portfolio-digest corporate-actions consolidation NZX:EXM --date 2024-07-01 \
  --reference FICTIONAL-CONSOLIDATION --details '{"before":"10","after":"1"}'
portfolio-digest corporate-actions rights_entitlement NZX:EXMRG --portfolio Personal \
  --date 2024-08-01 --reference FICTIONAL-RIGHTS --details '{"quantity":"10"}'
portfolio-digest corporate-actions rights_take_up NZX:EXMRG --portfolio Personal \
  --date 2024-08-15 --reference FICTIONAL-EXERCISE \
  --details '{"quantity":"6","target":"NZX:EXM","units":"6","price":"2"}'
portfolio-digest corporate-actions rights_lapse NZX:EXMRG --portfolio Personal \
  --date 2024-08-16 --reference FICTIONAL-LAPSE --details '{"quantity":"4"}'
portfolio-digest corporate-actions rename NZX:EXM --date 2024-09-01 \
  --reference FICTIONAL-RENAME --details '{"code":"EXNEW","name":"New Example"}'
portfolio-digest corporate-actions delisting NZX:EXNEW --date 2024-10-01 \
  --reference FICTIONAL-DELIST
```

Rights entitlement creates a rights instrument when needed. Take-up consumes rights and
buys target shares at the subscription price; `units` defaults to the rights consumed.
Lapse removes unused rights. Neither can consume more rights than held. Events and their
terms are retained in `corporate_actions`. Renames keep the same instrument ID and record
the former code and name; older broker files resolve to that instrument. Delisting records
history and does not assume the holding is worthless. Record a verified disposal or
valuation separately. Rename commands update the current listing immediately, so record
renames once effective. Set Yahoo symbol overrides again when needed.

## Shared returns report

```sh
portfolio-digest returns --portfolio Personal --date 2024-12-31
portfolio-digest returns --portfolio Personal --date 2024-12-31 --gross-up
```

The shared `returns.report` model exposes units, weighted-average cost base, market value,
net dividends, gross dividends, withholding, a separate imputation gross-up column and
lifetime total return. Totals use net dividends by default; `--gross-up` adds NZ imputation
credits. Franking credits never enter totals. DRP income and the reinvestment purchase are
both included, preserving the distinction between earned income and invested cash.

Amounts convert to portfolio base currency at each cash flow's date, using broker FX
when given, otherwise the latest stored FX rate on or before that date. Valuations use
the latest raw close on or before the report date, adjusted for any later recorded split.
Missing prices or FX raise an explicit error. This lifetime monetary gain is market value
plus sale proceeds plus recognised income minus purchases; it is not a time-weighted,
money-weighted or tax return. Report labels use current instrument names/codes. Email
and dashboard consumers can reuse the same report model when introduced.
