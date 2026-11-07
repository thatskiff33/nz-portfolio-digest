# Period returns

`portfolio-digest returns --portfolio "Example" --period week` reports each holding,
each exchange subtotal and the portfolio in its base currency (normally NZD). The
engine in `portfolio_digest.returns` is pure: pass immutable typed inputs to
`calculate_returns`. `returns_loader` reads the existing trade, income, price and FX
tables. It does not fetch market data or write valuations.

## Dates and valuations

A period is **(opening date, closing date]**. Opening value is the position after all
trades on the opening date, at that date's close and FX. Trades and paid income on
the closing date count. This avoids double counting adjacent periods. Use the day
before the first purchase to include that purchase's execution return.

- `week`: seven calendar days back to today.
- `month`: the same day of the previous calendar month, clamped to its last day.
- `ytd`: 31 December's close to today.
- `all`: the day before the first trade to today, including purchase fees.
- `YYYY-MM-DD:YYYY-MM-DD`: explicit opening and closing dates.

Today uses the configured `TIMEZONE` (Pacific/Auckland by default). Empty portfolios
return zero amounts and undefined percentages. A zero-length range reports no
change. Holdings with opening units, trades in the range or dividend income in
the range are included. Holdings fully sold during the range remain; those sold before it
are omitted unless they receive income in the range. Quantities come from trades;
valuation is quantity × local close × FX. FX is base currency per local currency unit. The engine accepts other
portfolio base currencies too.

The latest observation **on or before** a valuation date is used. An observation
marked stale by its provider, or a missing trading day since the observation, produces a flag on the holding, market and portfolio. Prices
use the exchange calendar, so weekends and exchange holidays alone do not cause
a flag. FX uses its weekday calendar, including weekdays when the exchange is
closed. Future observations are never used. For live valuations with a supplied aware instant,
freshness expects only closes already due at that instant, independently for each
exchange and FX. Date-only calculations keep completed-day semantics and never read
the clock.
A missing price or FX with no prior observation stops the report with a clear
error rather than treating the holding as worthless. A genuine zero price is
valid. Fully sold holdings need no closing price. Broker-supplied transaction FX
is used when available; otherwise the same carry-forward lookup applies.

Initial price and FX fetches and default backfills start seven calendar days before
the first trade, so a weekend or holiday opening can use the preceding observation.
An explicit backfill start remains authoritative. This bounded lookback does not
guarantee an observation during an unusually long suspension.

`portfolio-digest prices fetch` populates the stored price and FX tables. The
returns loader reads these tables directly without fetching remote data, and the
pure engine performs the carry-forward lookup, including the stored `is_stale` flag.

## Return attribution

For an unchanged parcel of quantity `q`, let `p0` and `p1` be its local entry and
exit prices and `f0` and `f1` the corresponding FX rates:

```text
capital gain  = q × (p1 - p0) × f0
currency gain = q × p1 × (f1 - f0)
combined gain = q × (p1 × f1 - p0 × f0)
```

The interaction between price and FX belongs to currency gain. An NZD security
in an NZD portfolio has FX 1 and zero currency gain. Price gains and FX gains can
have opposite signs.

Opening units form a parcel at the opening price and FX, resetting historic cost
for the requested period. Each purchase creates a new parcel at its execution
price and FX. Sales realise the oldest parcel first (FIFO); a partial sale splits
that parcel. Surviving parcels are valued at the closing price and FX. Same-day
trades apply a matching split first, then additions before removals, since trade
rows have no execution times. Ties retain input order; the loader orders them by
database ID. This defines FIFO among same-day additions deterministically and
avoids a false oversell when a sale was entered before its same-day purchase.
Buy and sell fees, expressed in trade currency, reduce capital gain at transaction
FX. Buy outlays include fees; sale proceeds exclude fees. Opening balances act
as purchases at their recorded price and FX when inside the period.

The independent accounting identity is:

```text
total = closing value + net sale proceeds - opening value - purchase outlays
        - net non-cash adjustments + dividends
net investment cash flow = purchase outlays - net sale proceeds
```

Dividends are net cash from `income` dividend and distribution rows, converted
at payment-date FX (or recorded income FX). Gross cash, withholding tax and NZ
imputation credits are separate report fields and CLI columns. Totals exclude
imputation by default; `--gross-up` adds it without changing the net Dividends
column. AU franking never enters totals. With gross-up enabled, add imputation to
the accounting identity above. Interest and other income are excluded. Income rows
alone do not create shares: imported DRPs also create a linked purchase trade.

Year-end summaries use this same net cash dividend basis for total return. Gross
dividends, withholding and imputation remain separate income/tax figures; they do
not increase the year-end performance total.

## Percentages and rounding

This is a simple, unannualised period return. The shared denominator for all four
components is **opening value + purchase outlays + non-cash value moved in**,
without time weighting. Non-cash value moved in is the gross value of positive
non-split adjustments (transfers in, gifts in and other value moved in) at their
recorded price and FX. Opening balances inside the period already count as purchase
outlays, so they are not counted twice. Net sales and non-cash removals are counted
in the return amount, but do not reduce the denominator: otherwise a full
liquidation at cost would create a zero denominator. Mid-period purchases and
transfers in contribute their full value. This explicit convention makes the
Sharesight-shaped columns reproducible; it is not a claim of parity with any
undocumented Sharesight calculation or annualisation method.

Every market and portfolio subtotal sums unrounded amounts and denominators,
then calculates its own percentages. It never averages holding percentages.
Zero denominators display `n/a`, including for dividend-only rows. All calculations
use Decimal. The CLI rounds only at display to 2 decimal places, half up, so
independently rounded components may differ from a displayed total by a cent.
Calculations use a local precision of at least 80 significant digits: the schema's
quantity, money and FX fields need up to 64 digits per product, with headroom for
subtraction and aggregation. A higher caller precision is preserved; the caller's
Decimal context is never modified. Display precision also accommodates large totals.

`Result` retains opening and closing values, purchases and sales separately.
Transactions retain dates and cash flows, leaving room for a separate
money-weighted return strategy later without changing simple-return attribution.

Signed ADJUSTMENT trades before or on the opening date count towards opening
quantity. Inside the period, an owner adjustment matching a downloaded Split row
rescales parcel units, preserving each parcel's total local cost and entry FX.
Match once per split date against units held before that day's trades, using the exact
cross-product `quantity_change × shares_before = held × (shares_after - shares_before)`.
A rounded owner adjustment that does not match exactly follows the non-split rule
below. Downloaded Split metadata alone never changes quantity. If units were held
immediately before a downloaded split and it has neither a matching adjustment nor
a registry action, reports flag "Split on DATE not yet recorded; add the adjustment".
This warning remains visible for unresolved events before the opening date, appears
in emails and `prices check`, and suppresses the portfolio-drop alert.

A registry action recorded with `corporate-actions` has explicit split identity,
including before the period's opening close. At the action, the resulting holding
total is rounded to the ledger's 8 decimal places, half-even. Parcels are rescaled
to that total, with any arithmetic residual assigned to the last parcel. Each
parcel retains its total local cost and entry FX. This unit rounding is separate
from money display rounding. Record any registry cash in lieu as a separate sale;
the engine does not infer cash or a gain from a fraction.

Recording either a registry split or a matching owner adjustment is refused when
the other exists within 10 calendar days either side, including the boundary days.
Older matching same-day pairs count once; differently dated legacy pairs produce
a warning to reconcile the entries in `docs/dividends.md`, without deleting or
re-dating ledger rows. Zero-fee splits need no transaction-date FX because no value
is converted. Carried prices are rescaled for all split sources strictly after the
selected observation and on or before valuation. A close dated on or after an action
is already on that basis and is never rescaled for that action.

Other positive adjustments add a parcel at the recorded trade price and FX;
negative ones remove units FIFO at that price and FX, realising the removed
parcel's price and currency movement. A recorded zero price is valid. These
transfers, gifts, DRPs and corrections are non-cash: signed value moved in or out
is retained as `Result.adjustments` and summed in subtotals. The gross value moved
in by positive adjustments is retained separately as `Result.adjustment_inflows`,
summed in subtotals and added to the denominator; removals never reduce it, just as
sales do not. Registry splits and consolidations move no value, so they add nothing.
Adjustment fees reduce capital and count as cash outlays. Income rows alone never
add units. The owner decision of 6 Nov 2026 (QS-023) supersedes issue #19's
recorded choice, which excluded adjustments from the denominator. The accounting
identity above is unchanged; only the denominator definition changed.

### Worked transfer example

Every figure is invented. A portfolio opens with one holding worth NZ$1,000. During
the period a second holding arrives by transfer in, recorded at NZ$100,000, and gains
NZ$10,000. The first holding is unchanged. Closing value is NZ$111,000.

| Row | Opening | Transfer in | Closing | Total return | Denominator | Total % |
|---|---:|---:|---:|---:|---:|---:|
| Existing holding | 1,000 | 0 | 1,000 | 0 | 1,000 | 0.00 |
| Transferred holding | 0 | 100,000 | 110,000 | 10,000 | 100,000 | 10.00 |
| Portfolio | 1,000 | 100,000 | 111,000 | 10,000 | 101,000 | 9.90 |

The identity gives `111,000 - 1,000 - 100,000 = 10,000`. Before this change the
portfolio denominator was NZ$1,000, showing 1000.00%, and the transferred holding
showed `n/a`.

Short positions are rejected. A trade currency different from its instrument
currency is also rejected until normalised, rather than mixing unlike units.

## Independently worked weekly example

Every security and figure below is invented. Each holding opens at NZ$100.
There are no adjustments, so the portfolio denominator remains NZ$400.
The fixture and assertions are in `tests/test_returns.py`.

| Market:code | Units | Local price start → end | FX start → end | Capital NZ$ (%) | Dividends NZ$ (%) | Currency NZ$ (%) | Total NZ$ (%) |
|---|---:|---|---|---:|---:|---:|---:|
| NZX:FERN | 10 | 10 → 12 | 1 → 1 | 20.00 (20.00) | 3.00 (3.00) | 0.00 (0.00) | 23.00 (23.00) |
| ASX:WATTLE | 10 | 8 → 9 | 1.25 → 1.20 | 12.50 (12.50) | 2.00 (2.00) | -4.50 (-4.50) | 10.00 (10.00) |
| NASDAQ:ORBIT | 5 | 10 → 12 | 2 → 2.1 | 20.00 (20.00) | 0.00 (0.00) | 6.00 (6.00) | 26.00 (26.00) |
| NYSE:CANOPY | 4 | 12.5 → 10 | 2 → 2.1 | -20.00 (-20.00) | 0.00 (0.00) | 4.00 (4.00) | -16.00 (-16.00) |
| Portfolio | | | | 32.50 (8.13) | 5.00 (1.25) | 5.50 (1.38) | 43.00 (10.75) |

For example, WATTLE capital is `10 × (9 - 8) × 1.25 = 12.50`; currency is
`10 × 9 × (1.20 - 1.25) = -4.50`. Its closing value is NZ$108 and its NZ$2
dividend gives NZ$10 total return. Portfolio closing value is NZ$438; adding
NZ$5 dividends to the NZ$38 value increase gives NZ$43.

## Shared parcel ledger

`portfolio_digest.parcels` provides FIFO addition and removal, split rescaling,
and dated FX lookup for period and year-end calculations. Trade ordering and
split matching are public helpers in `returns`.

Period parcels reset to opening market value and expense acquisition and split
fees immediately. Year-end parcels retain historical NZD acquisition cost,
including purchase fees and split-day fees allocated by surviving units. Sale
fees reduce proceeds in both reports. These fee treatments serve different
reporting purposes and must remain explicit when changing parcel replay.

The quantity summary returned by `position` retains its existing weighted-average
`cost_base` for compatibility. This scalar summary is separate from the FIFO
parcels used for period attribution and year-end realised gains.
