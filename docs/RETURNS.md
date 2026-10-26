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

Today uses the configured `TIMEZONE` (Pacific/Auckland by default). Empty portfolios return zero amounts and
undefined percentages. A zero-length range reports no change. All holdings with
trades through the end or dividend income in the range are included, including
fully sold holdings. Quantities come from trades; valuation is quantity × local
close × FX. FX is base currency per local currency unit. The engine accepts other
portfolio base currencies too.

The latest observation **on or before** a valuation date is used. A previous date,
or an observation marked stale by its provider, produces a flag on the holding,
market and portfolio. FX uses the same rule. Future observations are never used.
A missing price or FX with no prior observation stops the report with a clear
error rather than treating the holding as worthless. A genuine zero price is
valid. Fully sold holdings need no closing price. Broker-supplied transaction FX
is used when available; otherwise the same carry-forward lookup applies.

This checkout contains the price/FX schema but no service module from issue #3.
The loader therefore reads the existing tables directly and the pure engine
performs the carry-forward lookup, including the stored `is_stale` flag.

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
trades retain input order; the loader orders by trade date then database ID.
Buy and sell fees, expressed in trade currency, reduce capital gain at transaction
FX. Buy outlays include fees; sale proceeds exclude fees. Opening balances act
as purchases at their recorded price and FX when inside the period.

The independent accounting identity is:

```text
total = closing value + net sale proceeds - opening value - purchase outlays + dividends
net investment cash flow = purchase outlays - net sale proceeds
```

Dividends are gross cash from `income` dividend and distribution rows, converted
at payment-date FX (or recorded income FX). They include withholding tax but
exclude imputation credits. Interest and other income are excluded. Payment-date
recognition and tax/DRP detail can evolve with issue #7. Income rows alone do not
create shares: reinvested units must also be recorded in trade history.

## Percentages and rounding

This is a simple, unannualised period return. The shared denominator for all four
components is **opening value + purchase outlays**, without time weighting.
Net sales are counted in the return amount, but do not reduce the denominator:
otherwise a full liquidation at cost would create a zero denominator. Mid-period
purchases contribute their full outlay. This explicit convention makes the
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

Adjustments and short positions are rejected with an error. The schema's signed
adjustment is ambiguous (split, transfer or correction), so assigning it an
invented cash flow would silently distort performance. A corporate-action model
is needed to support these safely. A trade currency different from its instrument
currency is also rejected until normalised, rather than mixing unlike units.

## Independently worked weekly example

Every security and figure below is invented. Each holding opens at NZ$100.
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
