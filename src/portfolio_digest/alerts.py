"""Alert evaluation using the shared returns engine and exchange calendars."""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from html import escape
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select

from portfolio_digest.config import ConfigError
from portfolio_digest.mailers import MailError, mail_settings, message, send
from portfolio_digest.models import AlertState, Instrument, Portfolio, Price, Split, Trade
from portfolio_digest.prices.calendars import FX, for_exchange
from portfolio_digest.returns import ReturnError, units_at
from portfolio_digest.returns_loader import load_returns
from portfolio_digest.returns_table import format_number as money
from portfolio_digest.scheduling import SchedulerStateError
from portfolio_digest.splits import rescale_price

logger = logging.getLogger(__name__)


class PushSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    alert_ntfy_url: SecretStr | None = None
    alert_ntfy_token: SecretStr | None = None
    alert_pushover_token: SecretStr | None = None
    alert_pushover_user: SecretStr | None = None


class _NoPushRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirected POST could leak the bearer token or portfolio figures.
        return None


_push_opener = build_opener(_NoPushRedirects())


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
        with _push_opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError
    except Exception:
        logger.warning("Optional push notification failed.")


# Close dates appended to a daily-move key: daily:{exchange}:{code}:{previous}:{latest}.
_CLOSE_PAIR = re.compile(r"\d{4}-\d{2}-\d{2}:\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Trigger:
    key: str
    text: str
    # Previous and latest close days for a daily move. Other rules leave this unset.
    close_dates: tuple[date, date] | None = None


def _skip(skipped: list[tuple[str, str]] | None, key: str, reason: str) -> None:
    logger.warning("Alert rule skipped (%s): %s.", key, reason)
    if skipped is not None:
        skipped.append((key, reason))


def evaluate(session, portfolio, config, now, skipped: list[tuple[str, str]] | None = None):
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
            for key in (f"daily:{label}", f"stale:{label}"):
                _skip(skipped, key, "holding units could not be calculated")
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
                    opening = rescale_price(opening, split.shares_before, split.shares_after)
            movement = (prices[end] / opening - 1) * Decimal(100)
            key = f"daily:{label}"
            states[key] = (
                Trigger(
                    key,
                    f"{label} moved {movement:+.2f}% ({start} to {end}); "
                    f"closing prices {opening} to {prices[end]} {instrument.currency} "
                    "(split-adjusted).",
                    close_dates=(start, end),
                )
                if abs(movement) > limits.daily_percent
                else None
            )
        else:
            _skip(
                skipped,
                f"daily:{label}",
                "adjacent closing prices are missing, stale or not positive",
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
    # Use a common completed valuation date. A quote is not missing just because
    # Auckland is already on the next date while an overseas market is still open.
    end = min([today] + [for_exchange(i.exchange).latest_closed_day(now) for i in instruments])
    if any(i.currency != portfolio.base_currency for i in instruments):
        end = min(end, FX.latest_closed_day(now))
    start = end - timedelta(days=rules.period_days)
    try:
        report = load_returns(session, portfolio.name, f"{start}:{end}", today=today)
        total = report.total
        if total.flags:
            _skip(skipped, "portfolio", "valuation is incomplete or stale")
        elif total.opening <= 0:
            _skip(skipped, "portfolio", "opening value is not positive")
        else:
            change = total.change / total.opening * Decimal(100)
            states["portfolio"] = (
                Trigger(
                    "portfolio",
                    f"Portfolio value changed {change:+.2f}% over {rules.period_days} days "
                    f"({start} to {end}): "
                    f"{money(total.opening)} to {money(total.closing)} {report.base_currency}.",
                )
                if change < -rules.portfolio_drop_percent
                else None
            )
    except ReturnError:
        _skip(skipped, "portfolio", "valuation could not be calculated")
    return states


def run_alerts(session, config, *, dry_run=False, now=None, checkpoint=None):
    """Commit each portfolio before moving to the next or sending optional push."""
    if not config.alerts.enabled:
        return []
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ConfigError("Alert checks require an aware datetime.")
    output = []
    failed = False
    portfolio_ids = session.scalars(select(Portfolio.id).order_by(Portfolio.id)).all()
    with localcontext() as context:
        context.prec = max(context.prec, 80)
        for portfolio_id in portfolio_ids:
            if not dry_run and checkpoint and portfolio_id in checkpoint.delivered:
                continue
            try:
                query = select(Portfolio).where(Portfolio.id == portfolio_id)
                if not dry_run:
                    query = query.with_for_update()
                portfolio = session.scalar(query)
                if portfolio is None:
                    continue
                text = _run_portfolio(session, portfolio, config, now, dry_run)
                if not dry_run:
                    # Includes cleared rules and evaluations with no email due.
                    session.commit()
            except SchedulerStateError:
                raise
            except Exception:
                session.rollback()
                failed = True
                logger.warning("Portfolio alert check failed.")
                continue
            if not dry_run:
                if checkpoint:
                    checkpoint.complete(portfolio_id)
                if text:
                    push(text)
            if text:
                output.append(text)
    if failed:
        raise MailError("Some portfolio alerts failed. Check alert data and delivery settings.")
    return output


def _delivery_key(trigger: Trigger) -> str:
    """Key written to alert state. A daily move includes its close-date pair."""
    if trigger.close_dates is None:
        return trigger.key
    previous_close, latest_close = trigger.close_dates
    return f"{trigger.key}:{previous_close.isoformat()}:{latest_close.isoformat()}"


def _saved_for_rule(saved: dict[str, AlertState], key: str) -> list[AlertState]:
    """Delivery rows for one rule.

    Daily moves match the holding key and any close-date suffix, so a row
    recorded before pairs were stored is still found. Other rules match exactly.
    """
    if not key.startswith("daily:"):
        row = saved.get(key)
        return [] if row is None else [row]
    prefix = f"{key}:"
    return [
        row
        for rule_key, row in saved.items()
        if rule_key == key
        or (rule_key.startswith(prefix) and _CLOSE_PAIR.fullmatch(rule_key[len(prefix) :]))
    ]


def _is_due(saved: dict[str, AlertState], trigger: Trigger, cooldown_hours, now) -> bool:
    """Whether this trigger should send.

    A daily move sends when its close-date pair has not been sent. Repeating the
    same pair, including a later fetch the same day or over a weekend, does not.
    Portfolio-drop and stale rules send once until they clear, then follow cooldown.
    """
    if trigger.close_dates is not None:
        return _delivery_key(trigger) not in saved
    previous = saved.get(trigger.key)
    if previous is None:
        return True
    if cooldown_hours is None:
        return False
    # SQLite returns naive timestamps; the stored instant is always UTC.
    sent_at = previous.sent_at.replace(tzinfo=previous.sent_at.tzinfo or UTC)
    return now - sent_at >= timedelta(hours=cooldown_hours)


def _record_delivery(session, portfolio, saved, trigger, now) -> None:
    if trigger.close_dates is not None:
        delivered = _delivery_key(trigger)
        for row in _saved_for_rule(saved, trigger.key):
            if row.rule_key != delivered:
                session.delete(row)
        session.add(AlertState(portfolio_id=portfolio.id, rule_key=delivered, sent_at=now))
        return
    previous = saved.get(trigger.key)
    if previous is None:
        session.add(AlertState(portfolio_id=portfolio.id, rule_key=trigger.key, sent_at=now))
    else:
        previous.sent_at = now


def _run_portfolio(session, portfolio, config, now, dry_run):
    states = {
        s.rule_key: s
        for s in session.scalars(select(AlertState).where(AlertState.portfolio_id == portfolio.id))
    }
    skipped: list[tuple[str, str]] = []
    evaluated = evaluate(session, portfolio, config, now, skipped)
    suppressed = ""
    if dry_run and skipped:
        lines = [f"Suppressed rules | {portfolio.name}"]
        lines.extend(f"{key}: {reason}" for key, reason in skipped)
        suppressed = "\n".join(lines)
    due = []
    for key, trigger in evaluated.items():
        if trigger is None:
            if not dry_run:
                for row in _saved_for_rule(states, key):
                    session.delete(row)
            continue
        if _is_due(states, trigger, config.alerts.cooldown_hours, now):
            due.append(trigger)
    if not due:
        return suppressed or None
    text = f"Portfolio alerts | {portfolio.name}\n\n" + "\n".join(t.text for t in due)
    text += "\n\nOpen your dashboard over Tailscale for details."
    if dry_run:
        return "\n\n".join(part for part in (suppressed, text) if part)
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
        _record_delivery(session, portfolio, states, trigger, now)
    return text
