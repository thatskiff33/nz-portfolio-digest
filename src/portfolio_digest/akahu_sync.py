"""Persist confirmation attempts separately from the authoritative trade ledger."""

import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from sqlalchemy import Engine, func, select

from portfolio_digest import markets
from portfolio_digest.akahu import AkahuClient, AkahuError, exact_units
from portfolio_digest.db import session_scope
from portfolio_digest.models import AkahuMapping, AkahuSnapshot, Instrument, Portfolio
from portfolio_digest.reconcile import Mismatch, ledger_units, reconcile
from portfolio_digest.validation import InvalidInput

LEDGER_PLACES = Decimal("1e-8")


def _text(item: dict, *keys: str) -> str | None:
    """The first non-blank string among keys. Passed-through fields vary by provider."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _portfolio_holdings(account: dict) -> list[dict[str, Any]]:
    """Holdings from the documented `meta.portfolio`. Akahu passes these fields through
    from each integration unchanged, so every field except units is optional here."""
    meta = account.get("meta")
    portfolio = meta.get("portfolio") if isinstance(meta, dict) else None
    if not isinstance(portfolio, list):
        # Missing is not the same as empty: never confirm zero units by accident.
        raise AkahuError(
            "Akahu returned no holdings for an investment account. Check the Personal App "
            "can read investment holdings, then rerun after the next refresh."
        )
    holdings = []
    for item in portfolio:
        if not isinstance(item, dict):
            raise AkahuError("Akahu returned an invalid holding.")
        units = next(
            (item[key] for key in ("quantity", "units", "shares") if item.get(key) is not None),
            None,
        )
        code = _text(item, "code", "symbol", "ticker")
        exchange = _text(item, "exchange", "market")
        name = _text(item, "name")
        try:
            exchange_code = markets.exchange_code(exchange) if exchange else None
        except InvalidInput:
            exchange_code = None
        try:
            instrument_code = markets.instrument_code(code) if code else None
        except InvalidInput:
            instrument_code = None
        holding_id = _text(item, "_id", "id")
        if holding_id is None and instrument_code:
            holding_id = f"code:{exchange_code or exchange or '-'}:{instrument_code}"
        elif holding_id is None and name:
            holding_id = f"name:{name}"
        if holding_id is None:
            raise AkahuError("Akahu returned a holding with no identifier, code or name.")
        holdings.append(
            {
                # Fits akahu_mappings.holding_id so any holding can be mapped manually.
                "holding_id": holding_id[:128],
                "name": name,
                "code": code,
                "exchange": exchange,
                "instrument_code": instrument_code,
                "exchange_code": exchange_code,
                "units": exact_units(units),
            }
        )
    return holdings


def _match(instruments: list[Instrument], holding: dict) -> int | None:
    """One instrument by code, and by exchange when Akahu names one. Otherwise unmapped."""
    code = holding["instrument_code"]
    if not code or (holding["exchange"] and not holding["exchange_code"]):
        return None
    matches = [
        i
        for i in instruments
        if i.code == code
        and (holding["exchange_code"] is None or i.exchange == holding["exchange_code"])
    ]
    return matches[0].id if len(matches) == 1 else None


def _account_metadata(account: dict) -> dict:
    balance = account.get("balance")
    refreshed = account.get("refreshed")
    if balance is not None and not isinstance(balance, dict):
        raise AkahuError("Akahu returned an invalid balance.")
    if refreshed is not None and not isinstance(refreshed, dict):
        raise AkahuError("Akahu returned invalid refresh metadata.")
    cleaned_balance = None
    if balance is not None:
        currency = balance.get("currency")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise AkahuError("Akahu returned an invalid balance currency.")
        cleaned_balance = {"currency": currency.upper()}
        for key in ("current", "available", "limit"):
            if key in balance:
                cleaned_balance[key] = str(exact_units(balance[key]))
    cleaned_refresh = {}
    for key in ("balance", "meta", "holdings"):
        value = (refreshed or {}).get(key)
        if value is not None:
            if not isinstance(value, str):
                raise AkahuError("Akahu returned invalid refresh metadata.")
            cleaned_refresh[key] = value
    return {"balance": cleaned_balance, "refreshed": cleaned_refresh}


def sync(
    engine: Engine, portfolio_name: str, client: AkahuClient, account_ids: list[str]
) -> tuple[int, list[Mismatch], int]:
    with session_scope(engine) as session:
        portfolio = session.scalar(
            select(Portfolio).where(func.lower(Portfolio.name) == portfolio_name.lower())
        )
        if portfolio is None:
            raise AkahuError("Portfolio not found. Add it before syncing.")
        portfolio_id = portfolio.id
        snapshot = AkahuSnapshot(portfolio_id=portfolio_id)
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id

    try:
        accounts = [a for a in client.accounts() if a.get("type") == "INVESTMENT"]
        if account_ids:
            accounts = [a for a in accounts if a.get("_id") in account_ids]
            if {a.get("_id") for a in accounts} != set(account_ids):
                raise AkahuError("A configured investment account is missing.")
        if not accounts:
            raise AkahuError("No investment accounts available; cannot confirm holdings.")
        observations = []
        seen_accounts = set()
        for account in accounts:
            account_id = account.get("_id")
            if not isinstance(account_id, str) or not account_id or account_id in seen_accounts:
                raise AkahuError("Akahu returned an invalid or duplicate account.")
            seen_accounts.add(account_id)
            if account.get("status") != "ACTIVE":
                raise AkahuError("An investment account is inactive. Reconnect it in Akahu.")
            holdings = _portfolio_holdings(account)
            observations.append(
                {
                    "account_id": account_id,
                    **_account_metadata(account),
                    "holdings": holdings,
                }
            )
        with session_scope(engine) as session, localcontext() as context:
            context.prec = 50
            instruments = list(session.scalars(select(Instrument)))
            mappings = {
                (m.account_id, m.holding_id): m.instrument_id
                for m in session.scalars(
                    select(AkahuMapping).where(AkahuMapping.portfolio_id == portfolio_id)
                )
            }
            observed: dict[int, Decimal] = {}
            unmapped = []
            cleaned = []
            for account in observations:
                seen = set()
                rows = []
                for holding in account["holdings"]:
                    holding_id = holding["holding_id"]
                    if holding_id in seen:
                        raise AkahuError("Akahu returned an invalid or duplicate holding.")
                    seen.add(holding_id)
                    # The ledger stores 8 decimal places, so compare at that precision.
                    compared = holding["units"].quantize(LEDGER_PLACES, rounding=ROUND_HALF_EVEN)
                    instrument_id = mappings.get((account["account_id"], holding_id))
                    if instrument_id is None:
                        instrument_id = _match(instruments, holding)
                    row = {
                        **holding,
                        "units": str(holding["units"]),
                        "compared_units": str(compared),
                        "instrument_id": instrument_id,
                    }
                    rows.append(row)
                    if instrument_id is not None:
                        observed[instrument_id] = observed.get(instrument_id, Decimal(0)) + compared
                    elif compared != 0:
                        # A zero-unit leftover (e.g. a fully sold fund) is not drift.
                        unmapped.append({"account_id": account["account_id"], **row})
                cleaned.append({**account, "holdings": rows})
            mismatches = reconcile(ledger_units(session, portfolio_id), observed)
            snapshot = session.get(AkahuSnapshot, snapshot_id)
            snapshot.status = "drift" if mismatches or unmapped else "matched"
            snapshot.finished_at = datetime.now(UTC)
            snapshot.observations = json.dumps(cleaned, default=str)
            snapshot.report = json.dumps(
                {
                    "unmapped": unmapped,
                    "mismatches": [
                        dict(
                            instrument_id=m.instrument_id,
                            ledger=str(m.ledger),
                            observed=str(m.observed),
                            difference=str(m.difference),
                        )
                        for m in mismatches
                    ],
                }
            )
        return snapshot_id, mismatches, len(unmapped)
    except Exception:
        with session_scope(engine) as session:
            snapshot = session.get(AkahuSnapshot, snapshot_id)
            snapshot.status = "failed"
            snapshot.finished_at = datetime.now(UTC)
        raise
