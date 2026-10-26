"""Alert evaluation using the shared returns engine and exchange calendars."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from html import escape
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select

from portfolio_digest.config import ConfigError
from portfolio_digest.mailers import mail_settings, message, send
from portfolio_digest.models import AlertState, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.prices.calendars import for_exchange
from portfolio_digest.returns import ReturnError, units_at
from portfolio_digest.returns_loader import load_returns
from portfolio_digest.returns_table import format_number as money

logger = logging.getLogger(__name__)


class PushSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    alert_ntfy_url: SecretStr | None = None
    alert_ntfy_token: SecretStr | None = None
    alert_pushover_token: SecretStr | None = None
    alert_pushover_user: SecretStr | None = None


def push(text: str) -> None:
    """Optional HTTPS notifications. Never log URLs, payloads or provider exceptions."""
    try:
        settings = PushSettings()
    except ValidationError:
        logger.warning("Invalid alert push settings.")
        return
    try:
        if settings.alert_ntfy_url:
            url = settings.alert_ntfy_url.get_secret_value()
            if urlsplit(url).scheme != "https":
                raise ValueError
            headers = {}
            if settings.alert_ntfy_token:
                headers["Authorization"] = "Bearer " + settings.alert_ntfy_token.get_secret_value()
            request = Request(url, data=text.encode(), headers=headers, method="POST")  # noqa: S310
        elif settings.alert_pushover_token and settings.alert_pushover_user:
            request = Request(
                "https://api.pushover.net/1/messages.json",
                data=urlencode(
                    {
                        "token": settings.alert_pushover_token.get_secret_value(),
                        "user": settings.alert_pushover_user.get_secret_value(),
                        "message": text,
                    }
                ).encode(),
                method="POST",
            )
        else:
            return
        # urllib avoids httpx request logging, which could expose the secret topic URL.
        with urlopen(request, timeout=10) as response:  # noqa: S310
            if response.status != 200:
                raise ValueError
    except Exception:
        logger.warning("Alert email sent, but the optional push failed.")


@dataclass(frozen=True)
class Trigger:
    key: str
    text: str


def evaluate(session, portfolio, config, now):
    """Return known rule states. Missing valuation data leaves that rule unchanged."""
    rules = config.alerts
    states: dict[str, Trigger | None] = {}
    today = now.astimezone(ZoneInfo(config.timezone)).date()
    instruments = session.scalars(
        select(Instrument).join(Trade).where(Trade.portfolio_id == portfolio.id).distinct()
    ).all()
    for instrument in instruments:
        label = f"{instrument.exchange}:{instrument.code}"
        override = rules.holdings.get(label)
        limits = (
            rules.model_copy(update=override.model_dump(exclude_unset=True)) if override else rules
        )
        calendar = for_exchange(instrument.exchange)
        end = calendar.latest_closed_day(now)
        start = calendar.last_trading_day(end - timedelta(days=1))
        trades = session.scalars(
            select(Trade)
            .where(
                Trade.portfolio_id == portfolio.id,
                Trade.instrument_id == instrument.id,
                Trade.trade_date <= today,
            )
            .order_by(Trade.trade_date, Trade.id)
        ).all()
        splits = session.scalars(
            select(Split)
            .where(
                Split.instrument_id == instrument.id,
                Split.split_date <= today,
            )
            .order_by(Split.split_date)
        ).all()
        # Unit ownership does not require a price or FX history. In particular,
        # a newly bought holding with no prices must still get a stale alert.
        try:
            units = units_at(trades, splits, today)
        except ReturnError:
            continue
        if units <= 0:
            states[f"daily:{label}"] = states[f"stale:{label}"] = None
            continue
        # Compare actual adjacent closes in the listing currency. Trades, fees and
        # FX do not change a quoted price. Rescale the opening price for splits.
        prices = {
            price.price_date: price.close
            for price in session.scalars(
                select(Price).where(
                    Price.instrument_id == instrument.id,
                    Price.price_date.in_((start, end)),
                    Price.is_stale.is_(False),
                )
            )
        }
        if start in prices and end in prices and prices[start] > 0:
            opening = prices[start]
            for split in splits:
                if start < split.split_date <= end:
                    opening = opening * split.shares_before / split.shares_after
            movement = (prices[end] / opening - 1) * Decimal(100)
            key = f"daily:{label}"
            states[key] = (
                Trigger(
                    key,
                    f"{label} moved {movement:+.2f}% ({start} to {end}); "
                    f"closing prices {opening} to {prices[end]} {instrument.currency} "
                    "(split-adjusted).",
                )
                if abs(movement) > limits.daily_percent
                else None
            )
        fresh = session.scalar(
            select(func.max(Price.price_date)).where(
                Price.instrument_id == instrument.id,
                Price.price_date <= end,
                Price.is_stale.is_(False),
            )
        )
        if fresh is None:
            fresh = session.scalar(
                select(func.min(Trade.trade_date)).where(
                    Trade.portfolio_id == portfolio.id,
                    Trade.instrument_id == instrument.id,
                    Trade.trade_date <= end,
                )
            )
            since = "no fresh price since first trade"
        else:
            since = "last fresh price"
        if fresh is not None:
            age = sum(1 for _ in calendar.trading_days(fresh + timedelta(days=1), end))
            key = f"stale:{label}"
            states[key] = (
                Trigger(key, f"{label} price stale for {age} trading days; {since} {fresh}.")
                if age > limits.stale_trading_days
                else None
            )
    start = today - timedelta(days=rules.period_days)
    try:
        report = load_returns(session, portfolio.name, f"{start}:{today}", today=today)
        total = report.total
        if not total.flags and total.opening > 0:
            change = total.change / total.opening * Decimal(100)
            states["portfolio"] = (
                Trigger(
                    "portfolio",
                    f"Portfolio value changed {change:+.2f}% over {rules.period_days} days: "
                    f"{money(total.opening)} to {money(total.closing)} {report.base_currency}.",
                )
                if change < -rules.portfolio_drop_percent
                else None
            )
    except ReturnError:
        pass
    return states


def run_alerts(session, config, *, dry_run=False, now=None):
    """Deliver and persist per portfolio under a lock shared by concurrent checks."""
    if not config.alerts.enabled:
        return []
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ConfigError("Alert checks require an aware datetime.")
    output = []
    portfolios = select(Portfolio).order_by(Portfolio.id)
    if not dry_run:
        portfolios = portfolios.with_for_update()
    with localcontext() as context:
        context.prec = max(context.prec, 80)
        for portfolio in session.scalars(portfolios).all():
            states = {
                s.rule_key: s
                for s in session.scalars(
                    select(AlertState).where(AlertState.portfolio_id == portfolio.id)
                )
            }
            evaluated = evaluate(session, portfolio, config, now)
            due = []
            for key, trigger in evaluated.items():
                previous = states.get(key)
                if trigger is None:
                    if previous is not None and not dry_run:
                        session.delete(previous)
                    continue
                cooldown = config.alerts.cooldown_hours
                if previous is None or (
                    cooldown is not None
                    # SQLite returns naive timestamps; the stored instant is always UTC.
                    and now - previous.sent_at.replace(tzinfo=previous.sent_at.tzinfo or UTC)
                    >= timedelta(hours=cooldown)
                ):
                    due.append(trigger)
            if not due:
                continue
            text = f"Portfolio alerts | {portfolio.name}\n\n" + "\n".join(t.text for t in due)
            text += "\n\nOpen your dashboard over Tailscale for details."
            output.append(text)
            if dry_run:
                continue
            settings = mail_settings()
            recipients = config.recipients or ([settings.mail_owner] if settings.mail_owner else [])
            html = (
                '<html lang="en-NZ"><body style="font-family:Arial,sans-serif">'
                + "".join(f"<p>{escape(line)}</p>" for line in text.splitlines() if line)
                + "</body></html>"
            )
            send(
                settings,
                message(settings.mail_from, recipients, "Portfolio alerts", html, text),
                recipients,
            )
            for trigger in due:
                previous = states.get(trigger.key)
                if previous is None:
                    session.add(
                        AlertState(portfolio_id=portfolio.id, rule_key=trigger.key, sent_at=now)
                    )
                else:
                    previous.sent_at = now
            session.flush()
            push(text)
    return output
