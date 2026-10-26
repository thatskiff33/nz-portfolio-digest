"""Command-line entry point: `portfolio-digest <command>`."""

import argparse
import sys
from datetime import date

from alembic.util import CommandError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from portfolio_digest import __version__, migrate, validation
from portfolio_digest.config import ConfigError, get_settings
from portfolio_digest.db import make_engine, session_scope
from portfolio_digest.models import PORTFOLIO_NAME_UNIQUE, Portfolio
from portfolio_digest.returns import ReturnError, period_range
from portfolio_digest.returns_loader import load_returns
from portfolio_digest.returns_table import render_table


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


def cmd_returns(args: argparse.Namespace) -> int:
    try:
        validation.portfolio_name(args.portfolio)
        period_range(args.period, date.today())
        with session_scope(make_engine()) as session:
            report = load_returns(session, args.portfolio, args.period)
        print(render_table(report))
        return 0
    except (ReturnError, validation.InvalidInput) as exc:
        _fail(str(exc))
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio-digest")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="Bring the database up to date").set_defaults(func=cmd_migrate)
    sub.add_parser("health", help="Check the database is reachable and migrated").set_defaults(
        func=cmd_health
    )

    returns = sub.add_parser("returns", help="Report capital, dividend and currency returns")
    returns.add_argument("--portfolio", required=True)
    returns.add_argument("--period", required=True, help="week|month|ytd|all|YYYY-MM-DD:YYYY-MM-DD")
    returns.set_defaults(func=cmd_returns)

    portfolio = sub.add_parser("portfolio", help="Manage portfolios")
    psub = portfolio.add_subparsers(dest="portfolio_command", required=True)
    add = psub.add_parser("add", help="Add a portfolio")
    add.add_argument("name")
    add.add_argument(
        "--base-currency", help="ISO currency code (default: the BASE_CURRENCY setting, NZD)"
    )
    add.set_defaults(func=cmd_portfolio_add)
    psub.add_parser("list", help="List portfolios").set_defaults(func=cmd_portfolio_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        _fail(str(exc))
    except CommandError as exc:
        # Alembic's own messages, e.g. a revision it cannot find. They never include the URL.
        _fail(f"Migration failed: {exc}")
    except SQLAlchemyError as exc:
        # Only the exception type: the message can include connection details or row data.
        _fail(f"Database error ({type(exc).__name__}). Run `portfolio-digest health` to check.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
