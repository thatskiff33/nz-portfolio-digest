"""Where prices and exchange rates come from.

Every source sits behind the `PriceSource` or `FxSource` interface, so a paid service
(EODHD, Twelve Data) can replace Yahoo by adding a class here and changing PRICE_SOURCE or
FX_SOURCES in the settings. Nothing else needs to know which source is in use.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Context, Decimal
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from portfolio_digest.prices.http import JsonFetcher, SourceError
from portfolio_digest.prices.symbols import yahoo_symbol


@dataclass(frozen=True)
class Listing:
    """What a source needs to know to find an instrument."""

    exchange: str
    code: str
    # Hand-set Yahoo symbol for odd cases; see `symbols.py`.
    yahoo_symbol: str | None = None


@dataclass
class PriceHistory:
    """Raw daily closes in major currency units, plus splits used to restore them."""

    closes: dict[date, Decimal] = field(default_factory=dict)
    # Split date -> (shares before, shares after), e.g. (1, 4) for a 4-for-1 split.
    splits: dict[date, tuple[Decimal, Decimal]] = field(default_factory=dict)


class PriceSource(Protocol):
    name: str

    def symbol(self, listing: Listing) -> str:
        """The source's own symbol for the listing. Raises ValueError if it has none."""
        ...

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        currency: str | None = None,
        split_start: date | None = None,
    ) -> PriceHistory:
        """Raw closes in `currency` from `start` to `end`, inclusive.

        Include split events since `split_start` so callers can detect historical changes.
        """
        ...


class FxSource(Protocol):
    name: str

    def rates(self, currency: str, base: str, start: date, end: date) -> dict[date, Decimal]:
        """How many units of `base` one unit of `currency` buys, per day."""
        ...


# Yahoo sends prices as 32-bit floats, so 2.94 arrives as 2.940000057220459. Seven
# significant figures is all a 32-bit float holds, and rounding there recovers the real price.
_YAHOO_PRECISION = Context(prec=7)

YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"


class YahooSource:
    """Yahoo Finance's free chart data. Unofficial, so it is wrapped in retries and checks."""

    name = "yahoo"

    def __init__(self, fetcher: JsonFetcher) -> None:
        self.fetcher = fetcher

    def symbol(self, listing: Listing) -> str:
        return yahoo_symbol(listing.exchange, listing.code, listing.yahoo_symbol)

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        currency: str | None = None,
        split_start: date | None = None,
    ) -> PriceHistory:
        # Ask for a couple of days either side, since markets ahead of UTC (NZX) start
        # their day on the previous UTC date. Bars outside the range are dropped below.
        params = {
            "period1": _unix(min(start, split_start or start) - timedelta(days=2)),
            # Even a sold holding's old closes reflect splits up to the present.
            "period2": _unix(max(end, datetime.now(UTC).date()) + timedelta(days=2)),
            "interval": "1d",
            "events": "splits",
            "includePrePost": "false",
        }
        data = self.fetcher.get(YAHOO_CHART_URL.format(symbol=quote(symbol, safe="")), params)
        try:
            return _parse_chart(data, start, end, currency)
        except (
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
            ValueError,
            ArithmeticError,
            OSError,
        ):
            raise SourceError(
                f"Yahoo sent price data for {symbol} in an unexpected shape."
            ) from None

    def rates(self, currency: str, base: str, start: date, end: date) -> dict[date, Decimal]:
        # USDNZD=X is NZ$ per US$, which is exactly how rates are stored.
        rates = self.history(f"{currency}{base}=X", start, end, currency=base).closes
        if any(rate <= 0 for rate in rates.values()):
            raise SourceError(f"Yahoo sent an impossible {currency}/{base} rate.")
        return rates


def _unix(day: date) -> int:
    return int(datetime.combine(day, time(), tzinfo=UTC).timestamp())


def _parse_chart(data: Any, start: date, end: date, currency: str | None = None) -> PriceHistory:
    if not isinstance(data, dict):
        raise SourceError("Yahoo sent price data in an unexpected shape.")
    result = (data.get("chart") or {}).get("result")
    if not result:
        raise SourceError("Yahoo sent no price data.")
    chart = result[0]
    meta = chart.get("meta", {})
    tz = _zone(meta.get("exchangeTimezoneName"))
    quoted_currency = meta.get("currency")
    major_currency, scale = {
        "GBp": ("GBP", Decimal("0.01")),
        "ZAc": ("ZAR", Decimal("0.01")),
        "ILA": ("ILS", Decimal("0.01")),
    }.get(quoted_currency, (quoted_currency, Decimal("1")))
    if currency is not None and major_currency != currency:
        raise SourceError(f"Yahoo quote currency does not match {currency}.")
    history = PriceHistory()
    # Yahoo's quote.close is retrospectively split-adjusted, even without adjclose.
    # Undo all later splits, including those beyond the requested close range.
    for split in ((chart.get("events") or {}).get("splits") or {}).values():
        day = datetime.fromtimestamp(split["date"], tz).date()
        before, after = Decimal(str(split["denominator"])), Decimal(str(split["numerator"]))
        if not before.is_finite() or not after.is_finite() or before <= 0 or after <= 0:
            raise SourceError("Yahoo sent an impossible split ratio.")
        history.splits[day] = (before, after)
    closes = chart["indicators"]["quote"][0].get("close") or []
    for stamp, close in zip(chart.get("timestamp") or [], closes, strict=False):
        day = datetime.fromtimestamp(stamp, tz).date()
        if close is None or not start <= day <= end:
            continue
        value = Decimal(close)
        if not value.is_finite() or value < 0:
            raise SourceError(f"Yahoo sent an impossible price ({value}) for {day}.")
        value *= scale
        for split_day, (before, after) in sorted(history.splits.items()):
            if day < split_day:
                value = value * after / before
        history.closes[day] = _YAHOO_PRECISION.plus(value)
    return history


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


FRANKFURTER_URL = "https://api.frankfurter.dev/v1/{start}..{end}"


class FrankfurterSource:
    """European Central Bank reference rates via Frankfurter. Free, keyless, working days only."""

    name = "frankfurter"

    def __init__(self, fetcher: JsonFetcher) -> None:
        self.fetcher = fetcher

    def rates(self, currency: str, base: str, start: date, end: date) -> dict[date, Decimal]:
        url = FRANKFURTER_URL.format(start=start.isoformat(), end=end.isoformat())
        data = self.fetcher.get(url, {"base": currency, "symbols": base})
        try:
            rates = {date.fromisoformat(day): Decimal(v[base]) for day, v in data["rates"].items()}
        except (KeyError, TypeError, AttributeError, ValueError, ArithmeticError):
            raise SourceError(
                f"Frankfurter sent {currency}/{base} rates in an unexpected shape."
            ) from None
        if any(not r.is_finite() or r <= 0 for r in rates.values()):
            raise SourceError(f"Frankfurter sent an impossible {currency}/{base} rate.")
        # Frankfurter includes the working day before `start` when `start` is a holiday.
        return {day: rate for day, rate in rates.items() if start <= day <= end}


PRICE_SOURCES: dict[str, type[PriceSource]] = {"yahoo": YahooSource}
FX_SOURCES: dict[str, type[FxSource]] = {"yahoo": YahooSource, "frankfurter": FrankfurterSource}
