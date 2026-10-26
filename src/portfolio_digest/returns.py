"""Shared exact-decimal holdings and lifetime return report.

Splits affect units, never total cost. Same-day splits precede trades. Prices are raw,
unadjusted closes. Income is recognised on payment date, net of tax by default.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.importing import ImportFailure
from portfolio_digest.models import FxRate, Income, Instrument, Portfolio, Price, Split, Trade

ZERO = Decimal(0)


@dataclass
class Position:
    units: Decimal = ZERO
    cost_base: Decimal = ZERO
    purchases: Decimal = ZERO
    proceeds: Decimal = ZERO


def units_at(trades, splits, end: date) -> Decimal:
    """Quantity-only ledger, also usable for reconciling incomplete or short histories."""
    splits = list(splits)
    units = ZERO
    for trade in trades:
        if trade.trade_date > end:
            continue
        quantity = -trade.quantity if trade.trade_type == "sell" else trade.quantity
        for split in splits:
            if trade.trade_date < split.split_date <= end:
                quantity *= split.shares_after / split.shares_before
        units += quantity
    return units


def position(trades, splits, end: date, *, before_trades: bool = False) -> Position:
    """Replay dated trades and splits, refusing oversells rather than hiding bad history."""
    result = Position()
    events = [(s.split_date, 0, s) for s in splits if s.split_date <= end]
    events += [
        (t.trade_date, 1, t)
        for t in trades
        if t.trade_date <= end and not (before_trades and t.trade_date == end)
    ]
    for _, kind, row in sorted(events, key=lambda event: (event[0], event[1])):
        if kind == 0:
            result.units *= row.shares_after / row.shares_before
            continue
        amount = row.consideration if row.consideration is not None else row.quantity * row.price
        if row.trade_type == "sell" or row.quantity < 0:
            removed = abs(row.quantity)
            if removed > result.units:
                raise ImportFailure(
                    "Units removed exceed holdings; check trades and corporate actions."
                )
            result.cost_base -= result.cost_base * removed / result.units
            result.units -= removed
            result.proceeds += amount - row.fees if row.trade_type == "sell" else ZERO
        else:
            result.units += row.quantity
            result.cost_base += amount + row.fees
            result.purchases += amount + row.fees
    return result


def instrument_position(
    session: Session,
    portfolio_id: int,
    instrument_id: int,
    end: date,
    *,
    before_trades: bool = False,
) -> Position:
    return position(
        session.scalars(
            select(Trade)
            .where(Trade.portfolio_id == portfolio_id, Trade.instrument_id == instrument_id)
            .order_by(Trade.trade_date, Trade.id)
        ),
        session.scalars(select(Split).where(Split.instrument_id == instrument_id)),
        end,
        before_trades=before_trades,
    )


def fx(session, currency, base, day, broker_rate=None):
    if currency == base:
        return Decimal(1)
    if broker_rate is not None:
        return broker_rate
    rate = session.scalar(
        select(FxRate.rate)
        .where(
            FxRate.currency == currency,
            FxRate.base_currency == base,
            FxRate.rate_date <= day,
        )
        .order_by(FxRate.rate_date.desc())
        .limit(1)
    )
    if rate is None:
        raise ImportFailure(f"Missing {currency}/{base} exchange rate on or before {day}.")
    return rate


@dataclass(frozen=True)
class ReturnRow:
    instrument: str
    currency: str
    units: Decimal
    cost_base: Decimal
    market_value: Decimal
    dividends: Decimal
    gross_dividends: Decimal
    withholding_tax: Decimal
    imputation_gross_up: Decimal
    total_return: Decimal


def report(
    session: Session, portfolio: Portfolio, end: date, *, gross_up: bool = False
) -> list[ReturnRow]:
    """Base-currency lifetime gain: value + sale proceeds + income - purchases.

    No percentage is implied where the timing of contributions is unknown. Cost base
    is a weighted-average accounting cost in base currency, not a tax calculation.
    """
    ids = set(
        session.scalars(
            select(Trade.instrument_id).where(
                Trade.portfolio_id == portfolio.id, Trade.trade_date <= end
            )
        )
    )
    ids.update(
        session.scalars(
            select(Income.instrument_id).where(
                Income.portfolio_id == portfolio.id, Income.paid_date <= end
            )
        )
    )
    rows = []
    for instrument_id in sorted(ids):
        instrument = session.get(Instrument, instrument_id)
        trades = list(
            session.scalars(
                select(Trade)
                .where(
                    Trade.portfolio_id == portfolio.id,
                    Trade.instrument_id == instrument_id,
                    Trade.trade_date <= end,
                )
                .order_by(Trade.trade_date, Trade.id)
            )
        )
        # Convert cash flows at their own dates before replaying the cost ledger.
        from types import SimpleNamespace

        converted = []
        for trade in trades:
            rate = fx(
                session, trade.currency, portfolio.base_currency, trade.trade_date, trade.fx_rate
            )
            converted.append(
                SimpleNamespace(
                    trade_date=trade.trade_date,
                    trade_type=trade.trade_type,
                    quantity=trade.quantity,
                    price=trade.price * rate,
                    fees=trade.fees * rate,
                    consideration=trade.consideration * rate
                    if trade.consideration is not None
                    else None,
                )
            )
        splits = list(session.scalars(select(Split).where(Split.instrument_id == instrument_id)))
        holding = position(converted, splits, end)
        value = ZERO
        if holding.units:
            latest = session.scalar(
                select(Price)
                .where(Price.instrument_id == instrument_id, Price.price_date <= end)
                .order_by(Price.price_date.desc())
                .limit(1)
            )
            if latest is None:
                raise ImportFailure(f"Missing price for {instrument.code} on or before {end}.")
            close = latest.close
            for split in splits:
                if latest.price_date < split.split_date <= end:
                    close *= split.shares_before / split.shares_after
            value = (
                holding.units
                * close
                * fx(session, instrument.currency, portfolio.base_currency, end)
            )
        net = gross = tax = credits = ZERO
        for income in session.scalars(
            select(Income).where(
                Income.portfolio_id == portfolio.id,
                Income.instrument_id == instrument_id,
                Income.paid_date <= end,
            )
        ):
            rate = fx(
                session, income.currency, portfolio.base_currency, income.paid_date, income.fx_rate
            )
            net += income.net_amount * rate
            gross += income.gross_amount * rate
            tax += income.withholding_tax * rate
            credits += income.imputation_credits * rate
        rows.append(
            ReturnRow(
                f"{instrument.exchange}:{instrument.code}",
                portfolio.base_currency,
                holding.units,
                holding.cost_base,
                value,
                net,
                gross,
                tax,
                credits,
                value
                + holding.proceeds
                + net
                - holding.purchases
                + (credits if gross_up else ZERO),
            )
        )
    return rows
