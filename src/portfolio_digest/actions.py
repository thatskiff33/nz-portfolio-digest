"""Record registry corporate actions atomically, retaining their full instrument history."""

import hashlib
import json
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.importers.base import RowProblem, checked_text, parse_price, parse_quantity
from portfolio_digest.importing import ImportFailure
from portfolio_digest.markets import instrument_code
from portfolio_digest.models import CorporateAction, Instrument, Portfolio, Split, Trade
from portfolio_digest.returns import instrument_position

KINDS = (
    "split",
    "consolidation",
    "rights_entitlement",
    "rights_take_up",
    "rights_lapse",
    "rename",
    "delisting",
)


def record_action(
    session: Session,
    instrument: Instrument,
    day: date,
    kind: str,
    reference: str,
    details: dict,
    portfolio: Portfolio | None = None,
) -> CorporateAction:
    checked_text(reference, "Action reference")
    if kind not in KINDS or not reference.strip() or len(reference) > 128:
        raise RowProblem("Use a supported action kind and a reference of 1 to 128 characters.")
    # One lock serialises global split/rename changes for this listing.
    session.get(Instrument, instrument.id, with_for_update=True)
    if session.scalar(
        select(CorporateAction.id).where(
            CorporateAction.instrument_id == instrument.id,
            CorporateAction.external_ref == reference,
        )
    ):
        raise ImportFailure("That corporate-action reference is already recorded.")
    details = dict(details)
    if kind in ("split", "consolidation"):
        before = parse_quantity(str(details.get("before", "")))
        after = parse_quantity(str(details.get("after", "")))
        if (kind == "split" and after <= before) or (kind == "consolidation" and after >= before):
            raise RowProblem("Split must increase units; consolidation must reduce units.")
        split = session.get(Split, (instrument.id, day))
        if split and (split.shares_before != before or split.shares_after != after):
            raise ImportFailure("A different split ratio is already recorded on this date.")
        if split is None:
            session.add(
                Split(
                    instrument_id=instrument.id,
                    split_date=day,
                    shares_before=before,
                    shares_after=after,
                    source="registry",
                )
            )
        else:
            split.source = "registry"
    elif kind.startswith("rights_"):
        if portfolio is None:
            raise RowProblem("Rights events require --portfolio.")
        session.get(Portfolio, portfolio.id, with_for_update=True)
        if instrument.kind != "rights":
            raise RowProblem("Record rights events against a rights instrument, e.g. AIRRG.")
        quantity = parse_quantity(str(details.get("quantity", "")))
        if kind != "rights_entitlement":
            available = instrument_position(session, portfolio.id, instrument.id, day).units
            if quantity > available:
                raise RowProblem("Cannot exercise or lapse more rights than held.")

        def trade(target, quantity, price, suffix, trade_type="adjustment"):
            session.add(
                Trade(
                    portfolio_id=portfolio.id,
                    instrument_id=target.id,
                    trade_date=day,
                    trade_type=trade_type,
                    quantity=quantity,
                    price=price,
                    currency=target.currency,
                    source="corporate_action",
                    external_ref=f"{instrument.id}:{hashlib.sha256(reference.encode()).hexdigest()}:{suffix}",
                )
            )

        trade(
            instrument,
            quantity if kind == "rights_entitlement" else -quantity,
            Decimal(0),
            "rights",
        )
        if kind == "rights_take_up":
            label = str(details.get("target", ""))
            parts = label.split(":")
            if len(parts) != 2:
                raise RowProblem("Take-up requires target as EXCHANGE:CODE.")
            target = session.scalar(
                select(Instrument).where(
                    Instrument.exchange == parts[0].upper(), Instrument.code == parts[1].upper()
                )
            )
            if (
                target is None
                or target.id == instrument.id
                or target.currency != instrument.currency
            ):
                raise RowProblem(
                    "Take-up requires a different target instrument in the same currency."
                )
            units = parse_quantity(str(details.get("units", quantity)))
            price = parse_price(str(details.get("price", "")))
            trade(target, units, price, "take-up", "buy")
    elif kind == "rename":
        code = instrument_code(str(details.get("code", "")))
        name = checked_text(str(details.get("name") or instrument.name), "Instrument name")
        if len(name) > 255:
            raise RowProblem("Instrument name is too long.")
        details.update(old_code=instrument.code, old_name=instrument.name)
        instrument.code, instrument.name = code, name
        instrument.yahoo_symbol = None
    elif kind == "delisting":
        details["code"] = instrument.code
    action = CorporateAction(
        instrument_id=instrument.id,
        portfolio_id=portfolio.id if portfolio else None,
        effective_date=day,
        kind=kind,
        external_ref=reference,
        details=json.dumps(details, sort_keys=True, default=str),
        source="registry",
    )
    session.add(action)
    session.flush()
    return action
