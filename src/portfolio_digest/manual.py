"""Manual trades shared by the CLI and dashboard; corrections are signed adjustments."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from portfolio_digest.importers.base import (
    ParsedTrade,
    checked_text,
    listing,
    parse_date,
    parse_money,
    parse_price,
    parse_quantity,
    parse_security_name,
)
from portfolio_digest.importing import find_or_create_instrument, portfolio_named
from portfolio_digest.models import Instrument, Trade, TradeType
from portfolio_digest.actions import validate_split_adjustments
from portfolio_digest.returns import Transaction


def prepare_trade(
    *,
    exchange: str,
    code: str,
    currency: str | None = None,
    trade_date: str,
    trade_type: str,
    quantity: str,
    price: str,
    fees: str = "0",
    name: str | None = None,
    notes: str | None = None,
) -> ParsedTrade:
    kind = TradeType(trade_type)
    exchange, code, currency = listing(exchange, code, currency)
    day: date = parse_date(trade_date, "Trade date")
    units: Decimal = parse_quantity(
        quantity, "Quantity", allow_negative=kind == TradeType.ADJUSTMENT
    )
    unit_price = parse_price(price, "Price", currency=currency)
    fee = parse_money(fees, "Fees", currency=currency, allow_negative=False)
    name = parse_security_name(name)
    return ParsedTrade(
        where="manual entry",
        exchange=exchange,
        code=code,
        currency=currency,
        trade_date=day,
        trade_type=kind,
        quantity=units,
        price=unit_price,
        fees=fee,
        name=name,
        notes=checked_text(notes, "Notes") if notes else None,
        external_ref=f"manual:{uuid.uuid4().hex}",
    )


def add_trade(session: Session, portfolio_name: str, entry: ParsedTrade) -> Trade:
    portfolio = portfolio_named(session, portfolio_name, lock=True)
    instrument = find_or_create_instrument(
        session, entry.exchange, entry.code, entry.currency, entry.name
    )
    session.get(Instrument, instrument.id, with_for_update=True)
    if entry.trade_type == TradeType.ADJUSTMENT:
        validate_split_adjustments(
            session, instrument.id, portfolio_id=portfolio.id,
            adjustment=Transaction(entry.trade_date, "adjustment", entry.quantity, entry.price),
        )
    trade = Trade(
        portfolio_id=portfolio.id,
        instrument_id=instrument.id,
        trade_date=entry.trade_date,
        trade_type=entry.trade_type.value,
        quantity=entry.quantity,
        price=entry.price,
        currency=entry.currency,
        fees=entry.fees,
        source="manual",
        external_ref=entry.external_ref,
        notes=entry.notes,
    )
    session.add(trade)
    session.flush()
    return trade
