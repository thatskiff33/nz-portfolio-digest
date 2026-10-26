"""Command-line entry point: `portfolio-digest <command>`."""

import argparse
import sys
from datetime import UTC, date, datetime

from alembic.util import CommandError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from portfolio_digest import __version__, migrate, validation
from portfolio_digest.config import ConfigError, get_settings
from portfolio_digest.db import make_engine, session_scope
from portfolio_digest.models import PORTFOLIO_NAME_UNIQUE, Instrument, Portfolio
from portfolio_digest.prices.fetch import Sources, held_instruments, run_fetch
from portfolio_digest.prices.http import JsonFetcher, make_client
from portfolio_digest.prices.quality import check_data
from portfolio_digest.prices.sources import FX_SOURCES, PRICE_SOURCES
from portfolio_digest.prices.symbols import clean_symbol


def _fail(message: str) -> None:
    print(message, file=sys.stderr)


def cmd_migrate(_: argparse.Namespace) -> int:
    engine = make_engine()
    migrate.upgrade(engine)
    print(f"Database is at revision {migrate.current_revision(engine)}.")
    return 0


def cmd_health(_: argparse.Namespace) -> int:
    """Exit 0 only when the database is reachable and fully migrated."""
    try:
        engine = make_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        current, head = migrate.current_revision(engine), migrate.head_revision()
    except ConfigError as exc:
        _fail(f"FAIL: {exc}")
        return 1
    except SQLAlchemyError as exc:
        # Only the exception type: the message can include connection details.
        _fail(f"FAIL: cannot reach the database ({type(exc).__name__}).")
        return 1
    if current != head:
        if current is not None and not migrate.is_known_revision(current):
            _fail(
                f"FAIL: database is at revision {current}, which this version of "
                "portfolio-digest does not know. Is the app out of date?"
            )
        else:
            _fail(f"FAIL: database is at revision {current}, expected {head}. Run `migrate`.")
        return 1
    print(f"OK: portfolio-digest {__version__}, database at revision {current}.")
    return 0


def cmd_portfolio_add(args: argparse.Namespace) -> int:
    try:
        name = validation.portfolio_name(args.name)
        currency = args.base_currency
        if currency is None:
            currency = get_settings().base_currency
        currency = validation.currency_code(currency)
    except validation.InvalidInput as exc:
        _fail(str(exc))
        return 2
    try:
        with session_scope(make_engine()) as session:
            session.add(Portfolio(name=name, base_currency=currency))
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != (
            PORTFOLIO_NAME_UNIQUE
        ):
            raise
        _fail(f"There is already a portfolio called {name!r} (upper and lower case count as one).")
        return 1
    print(f"Added portfolio {name!r} ({currency}).")
    return 0


def cmd_portfolio_list(_: argparse.Namespace) -> int:
    with session_scope(make_engine()) as session:
        portfolios = session.scalars(select(Portfolio).order_by(Portfolio.name)).all()
        if not portfolios:
            print("No portfolios yet. Add one with `portfolio-digest portfolio add NAME`.")
        for p in portfolios:
            print(f"{p.id}\t{p.name}\t{p.base_currency}")
    return 0


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use an ISO date such as 2026-10-01") from None


def _instrument_label(value: str) -> str:
    parts = value.strip().upper().split(":")
    if len(parts) != 2 or not all(parts) or any(any(c.isspace() for c in part) for part in parts):
        raise argparse.ArgumentTypeError("use EXCHANGE:CODE, for example NZX:AIR")
    return ":".join(parts)


def cmd_prices_fetch(args: argparse.Namespace) -> int:
    settings = get_settings()
    with make_client() as client:
        fetcher = JsonFetcher(client)
        price_source = PRICE_SOURCES[settings.price_source](fetcher)
        fx_sources = [FX_SOURCES[name](fetcher) for name in settings.fx_source_names]
        report = run_fetch(
            make_engine(),
            Sources(price_source, fx_sources),
            now=datetime.now(UTC),
            base_currency=settings.base_currency,
            backfill=args.backfill or args.start is not None,
            start=args.start,
            only=args.instrument,
        )
    if not report.outcomes:
        print("No traded instruments found; nothing to fetch.")
        return 0
    for outcome in report.outcomes:
        print(outcome.describe())
    return 0 if report.ok else 1


def cmd_prices_symbol(args: argparse.Namespace) -> int:
    exchange, code = args.instrument.split(":", 1)
    with session_scope(make_engine()) as session:
        instrument = session.scalar(
            select(Instrument).where(Instrument.exchange == exchange, Instrument.code == code)
        )
        if instrument is None:
            _fail(f"No instrument called {args.instrument}.")
            return 1
        instrument.yahoo_symbol = None if args.clear else clean_symbol(args.symbol)
    if args.clear:
        print(f"Cleared the Yahoo symbol override for {args.instrument}.")
    else:
        print(f"Yahoo symbol for {args.instrument} is now {instrument.yahoo_symbol}.")
    return 0


def cmd_prices_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    now = datetime.now(UTC)
    with session_scope(make_engine()) as session:
        holdings = held_instruments(session, args.instrument)
        found = {holding.label for holding in holdings}
        missing = [label for label in args.instrument or [] if label not in found]
        findings = check_data(session, holdings, settings.base_currency, now)
    if missing:
        for label in missing:
            _fail(f"No traded instrument called {label}.")
        return 1
    if not findings:
        print("OK: no price or exchange-rate quality problems found.")
        return 0
    for finding in findings:
        print(finding.describe())
    print(f"Found {len(findings)} data-quality problem(s).")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio-digest")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="Bring the database up to date").set_defaults(func=cmd_migrate)
    sub.add_parser("health", help="Check the database is reachable and migrated").set_defaults(
        func=cmd_health
    )

    portfolio = sub.add_parser("portfolio", help="Manage portfolios")
    psub = portfolio.add_subparsers(dest="portfolio_command", required=True)
    add = psub.add_parser("add", help="Add a portfolio")
    add.add_argument("name")
    add.add_argument(
        "--base-currency", help="ISO currency code (default: the BASE_CURRENCY setting, NZD)"
    )
    add.set_defaults(func=cmd_portfolio_add)
    psub.add_parser("list", help="List portfolios").set_defaults(func=cmd_portfolio_list)

    prices = sub.add_parser("prices", help="Fetch and check daily prices and exchange rates")
    price_sub = prices.add_subparsers(dest="prices_command", required=True)

    fetch = price_sub.add_parser("fetch", help="Fetch prices and exchange rates")
    fetch.add_argument(
        "--backfill", action="store_true", help="Re-fetch from each instrument's first trade"
    )
    fetch.add_argument(
        "--start", type=_date, help="Re-fetch from this date instead of each first trade"
    )
    fetch.add_argument(
        "--instrument",
        action="append",
        type=_instrument_label,
        help="Only fetch EXCHANGE:CODE (repeatable)",
    )
    fetch.set_defaults(func=cmd_prices_fetch)

    symbol = price_sub.add_parser("symbol", help="Set an instrument's Yahoo symbol override")
    symbol.add_argument("instrument", type=_instrument_label)
    symbol_group = symbol.add_mutually_exclusive_group(required=True)
    symbol_group.add_argument("symbol", nargs="?", help="Yahoo symbol, for example AIRRG.NZ")
    symbol_group.add_argument("--clear", action="store_true", help="Return to automatic mapping")
    symbol.set_defaults(func=cmd_prices_symbol)

    check = price_sub.add_parser("check", help="Check stored market data for quality problems")
    check.add_argument(
        "--instrument",
        action="append",
        type=_instrument_label,
        help="Only check EXCHANGE:CODE (repeatable)",
    )
    check.set_defaults(func=cmd_prices_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        _fail(str(exc))
    except validation.InvalidInput as exc:
        _fail(str(exc))
        return 2
    except CommandError as exc:
        # Alembic's own messages, e.g. a revision it cannot find. They never include the URL.
        _fail(f"Migration failed: {exc}")
    except SQLAlchemyError as exc:
        # Only the exception type: the message can include connection details or row data.
        _fail(f"Database error ({type(exc).__name__}). Run `portfolio-digest health` to check.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
