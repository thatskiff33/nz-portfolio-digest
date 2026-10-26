"""Exact period monetary returns, independent of email or presentation.

The period includes both dates. Opening value is the close before the first date.
Capital gain is the local-currency value movement less net purchases (including fees),
translated at closing FX. Currency gain is the remaining NZD movement after actual
NZD flows. Dividends are net cash received, excluding imputation credits. Split
adjustments change units only; other adjustments and opening balances are external
flows at their recorded price (or market value when zero). DRP units use net income
as their reinvestment flow.
These are dollar returns, not annualised or time-weighted percentages.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.models import (
    FxRate,
    Income,
    Instrument,
    Portfolio,
    Price,
    Split,
    Trade,
    TradeType,
)
from portfolio_digest.prices.calendars import FX, for_exchange

ZERO = Decimal(0)


class ReportError(Exception):
    """A safe, actionable report failure without credentials or portfolio data."""


@dataclass(frozen=True)
class HoldingReturn:
    market: str
    code: str
    name: str
    currency: str
    units: Decimal
    price: Decimal
    value: Decimal
    capital_gain: Decimal
    dividends: Decimal
    currency_gain: Decimal
    opening_value: Decimal
    stale: bool = False

    @property
    def total_return(self) -> Decimal:
        return self.capital_gain + self.dividends + self.currency_gain


class PeriodEngine:
    """Load stored history once and reuse it for chart valuations."""

    def __init__(self, session: Session):
        self.portfolio_currencies = {
            p.id: p.base_currency for p in session.scalars(select(Portfolio))
        }
        self.instruments = list(session.scalars(select(Instrument).order_by(Instrument.id)))
        self.trades = list(session.scalars(select(Trade).order_by(Trade.trade_date, Trade.id)))
        self.income = list(session.scalars(select(Income).order_by(Income.paid_date, Income.id)))
        self.splits = {(s.instrument_id, s.split_date): s for s in session.scalars(select(Split))}
        self.prices: dict[int, list[Price]] = {}
        for row in session.scalars(select(Price).order_by(Price.price_date)):
            self.prices.setdefault(row.instrument_id, []).append(row)
        self.rates: dict[str, list[FxRate]] = {}
        for row in session.scalars(
            select(FxRate).where(FxRate.base_currency == "NZD").order_by(FxRate.rate_date)
        ):
            self.rates.setdefault(row.currency, []).append(row)

    def fx(self, currency: str, day: date) -> tuple[Decimal, bool]:
        if currency == "NZD":
            return Decimal(1), False
        row = next((r for r in reversed(self.rates.get(currency, [])) if r.rate_date <= day), None)
        if row is None:
            raise ReportError("Report cannot be valued: missing exchange rates. Run prices fetch.")
        expected = FX.latest_closed_day(datetime.combine(day, time.max, ZoneInfo("UTC")))
        return row.rate, row.is_stale or row.rate_date < expected

    def quote(self, instrument: Instrument, day: date) -> tuple[Decimal, bool]:
        row = next(
            (p for p in reversed(self.prices.get(instrument.id, [])) if p.price_date <= day), None
        )
        if row is None:
            raise ReportError(
                "Report cannot be valued: missing prices. Run prices fetch --backfill."
            )
        calendar = for_exchange(instrument.exchange)
        expected = calendar.latest_closed_day(
            datetime.combine(day, time.max, ZoneInfo(calendar.timezone))
        )
        return row.close, row.is_stale or row.price_date < expected

    def calculate(self, start: date, end: date) -> tuple[HoldingReturn, ...]:
        if start > end:
            raise ReportError("Report start must be on or before its end.")
        opening_day = start - timedelta(days=1)
        result = []
        for instrument in self.instruments:
            trades = [
                t for t in self.trades if t.instrument_id == instrument.id and t.trade_date <= end
            ]
            income = [
                i
                for i in self.income
                if i.instrument_id == instrument.id and start <= i.paid_date <= end
            ]
            units = opening_units = flows = base_flows = dividends = ZERO
            stale = False
            portfolio_units: dict[int, Decimal] = {}
            for trade in trades:
                signed = -trade.quantity if trade.trade_type == TradeType.SELL else trade.quantity
                before = portfolio_units.get(trade.portfolio_id, ZERO)
                portfolio_units[trade.portfolio_id] = before + signed
                if portfolio_units[trade.portfolio_id] < ZERO:
                    raise ReportError(
                        "Report cannot value negative holdings. Reconcile trade history."
                    )
                units += signed
                if trade.trade_date < start:
                    opening_units += signed
                else:
                    split = self.splits.get((instrument.id, trade.trade_date))
                    if (
                        trade.trade_type == TradeType.ADJUSTMENT
                        and split is not None
                        and signed == before * (split.shares_after / split.shares_before - 1)
                    ):
                        continue
                    reinvested = [
                        i
                        for i in income
                        if i.portfolio_id == trade.portfolio_id
                        and i.paid_date == trade.trade_date
                        and i.drp_quantity == signed
                    ]
                    payment = (
                        reinvested[0]
                        if (trade.trade_type == TradeType.ADJUSTMENT and len(reinvested) == 1)
                        else None
                    )
                    currency = payment.currency if payment is not None else trade.currency
                    fx, old = self.fx(currency, trade.trade_date)
                    recorded_fx = payment.fx_rate if payment is not None else trade.fx_rate
                    base_fx, base_old = self.fx(
                        self.portfolio_currencies.get(trade.portfolio_id, "NZD"), trade.trade_date
                    )
                    actual_fx = recorded_fx * base_fx if recorded_fx is not None else fx
                    stale |= base_old if recorded_fx is not None else False
                    local_fx, local_old = self.fx(instrument.currency, trade.trade_date)
                    amount = signed * trade.price + trade.fees
                    if payment is not None:
                        amount = payment.net_amount
                    # Gifts and transfers are flows, rather than investment gains.
                    elif (
                        trade.trade_type in {TradeType.OPENING_BALANCE, TradeType.ADJUSTMENT}
                        and trade.price == ZERO
                    ):
                        quote, quote_old = self.quote(instrument, trade.trade_date)
                        amount = signed * quote * local_fx / fx
                        stale |= quote_old
                    base_flows += amount * actual_fx
                    flows += amount * fx / local_fx
                    stale |= old or local_old
            if units < ZERO or opening_units < ZERO:
                raise ReportError("Report cannot value negative holdings. Reconcile trade history.")
            if not trades and not income:
                continue
            period_trades = any(t.trade_date >= start for t in trades)
            if units == ZERO and opening_units == ZERO and not period_trades and not income:
                continue
            closing_price, price_old = self.quote(instrument, end)
            closing_fx, fx_old = self.fx(instrument.currency, end)
            opening_local = opening = ZERO
            if opening_units:
                opening_price, old = self.quote(instrument, opening_day)
                opening_fx, old_fx = self.fx(instrument.currency, opening_day)
                opening_local = opening_units * opening_price
                opening = opening_local * opening_fx
                stale |= old or old_fx
            for payment in income:
                fx, old = self.fx(payment.currency, payment.paid_date)
                base_fx, base_old = self.fx(
                    self.portfolio_currencies.get(payment.portfolio_id, "NZD"), payment.paid_date
                )
                dividends += payment.net_amount * (
                    payment.fx_rate * base_fx if payment.fx_rate is not None else fx
                )
                stale |= base_old if payment.fx_rate is not None else False
                stale |= old
            value = units * closing_price * closing_fx
            capital = (units * closing_price - opening_local - flows) * closing_fx
            currency = value - opening - base_flows - capital
            result.append(
                HoldingReturn(
                    instrument.exchange,
                    instrument.code,
                    instrument.name,
                    instrument.currency,
                    units,
                    closing_price,
                    value,
                    capital,
                    dividends,
                    currency,
                    opening,
                    stale or price_old or fx_old,
                )
            )
        return tuple(sorted(result, key=lambda r: (r.market, r.code)))
