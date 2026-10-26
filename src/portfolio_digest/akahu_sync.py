"""Persist confirmation attempts separately from the authoritative trade ledger."""

import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext

from sqlalchemy import Engine, func, select

from portfolio_digest.akahu import AkahuClient, AkahuError, exact_units
from portfolio_digest.db import session_scope
from portfolio_digest.models import AkahuMapping, AkahuSnapshot, Instrument, Portfolio
from portfolio_digest.reconcile import Mismatch, ledger_units, reconcile


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
            holdings = client.holdings(account_id)
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
                    holding_id = holding.get("_id")
                    if not isinstance(holding_id, str) or not holding_id or holding_id in seen:
                        raise AkahuError("Akahu returned an invalid or duplicate holding.")
                    seen.add(holding_id)
                    units = exact_units(holding.get("units"))
                    code = holding.get("code", holding.get("symbol"))
                    exchange = holding.get("exchange")
                    if code is not None and not isinstance(code, str):
                        raise AkahuError("Akahu returned an invalid holding code.")
                    if exchange is not None and not isinstance(exchange, str):
                        raise AkahuError("Akahu returned an invalid holding exchange.")
                    instrument_id = mappings.get((account["account_id"], holding_id))
                    if instrument_id is None:
                        matches = [
                            i
                            for i in instruments
                            if code
                            and i.code == code.upper()
                            and (not exchange or i.exchange == exchange.upper())
                        ]
                        instrument_id = matches[0].id if len(matches) == 1 else None
                    row = {
                        "holding_id": holding_id,
                        "code": code,
                        "exchange": exchange,
                        "units": str(units),
                        "instrument_id": instrument_id,
                    }
                    rows.append(row)
                    if instrument_id is None:
                        unmapped.append({"account_id": account["account_id"], **row})
                    else:
                        observed[instrument_id] = observed.get(instrument_id, Decimal(0)) + units
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
