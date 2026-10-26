"""Which days each market trades, and which of those have already closed.

A day the market is shut (weekend or public holiday) is never a missing price, and a price
is only expected once the market has closed for the day.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import cached_property
from zoneinfo import ZoneInfo

import holidays


@dataclass(frozen=True)
class TradingCalendar:
    name: str
    timezone: str
    close: time
    # Market code in the `holidays` package (e.g. XNZE), or None for weekdays only.
    holiday_code: str | None = None

    @cached_property
    def _holidays(self) -> holidays.HolidayBase | None:
        if self.holiday_code is None:
            return None
        return holidays.financial_holidays(self.holiday_code)

    def is_trading_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return self._holidays is None or day not in self._holidays

    def trading_days(self, start: date, end: date) -> Iterator[date]:
        """Every trading day from `start` to `end`, both included."""
        day = start
        while day <= end:
            if self.is_trading_day(day):
                yield day
            day += timedelta(days=1)

    def last_trading_day(self, day: date) -> date:
        """The last trading day on or before `day`."""
        while not self.is_trading_day(day):
            day -= timedelta(days=1)
        return day

    def latest_closed_day(self, now: datetime) -> date:
        """The most recent trading day whose close is at or before `now` (timezone aware)."""
        local = now.astimezone(ZoneInfo(self.timezone))
        day = local.date() if local.time() >= self.close else local.date() - timedelta(days=1)
        return self.last_trading_day(day)


# Close times allow a little for the closing auction and for the source to catch up.
EXCHANGES: dict[str, TradingCalendar] = {
    "NZX": TradingCalendar("NZX", "Pacific/Auckland", time(17, 0), "XNZE"),
    "ASX": TradingCalendar("ASX", "Australia/Sydney", time(16, 15), "XASX"),
    "NYSE": TradingCalendar("NYSE", "America/New_York", time(16, 0), "XNYS"),
    "NASDAQ": TradingCalendar("NASDAQ", "America/New_York", time(16, 0), "XNAS"),
}

# Currencies trade around the clock on weekdays; the day's rate is fixed at 5pm New York.
FX = TradingCalendar("FX", "America/New_York", time(17, 0))

# Markets we do not know yet: weekdays, with the day counted as closed at midnight UTC.
_FALLBACK_CLOSE = time(23, 59, 59)


def for_exchange(exchange: str) -> TradingCalendar:
    return EXCHANGES.get(exchange) or TradingCalendar(exchange, "UTC", _FALLBACK_CLOSE)
