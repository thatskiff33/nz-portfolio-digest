"""Command-line entry point: `portfolio-digest <command>`."""

import argparse
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from alembic.util import CommandError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from portfolio_digest import __version__, migrate, validation
from portfolio_digest.config import ConfigError, get_gmail_settings, get_settings
from portfolio_digest.db import make_engine, session_scope
from portfolio_digest.gmail import AsbGmailImporter, GmailClient, GmailError, authorise
from portfolio_digest.importers.base import (
    FileProblem,
    RowProblem,
    listing,
    parse_date,
    parse_decimal,
    parse_money,
    parse_price,
)
from portfolio_digest.importing import (
    FILE_IMPORTERS,
    ImportFailure,
    import_file,
    import_gmail,
    portfolio_named,
    read_input,
    undo_import,
)
from portfolio_digest.models import (
    PORTFOLIO_NAME_UNIQUE,
    ImportRun,
    Instrument,
    Portfolio,
    Trade,
    TradeType,
)
from portfolio_digest.reconcile import compare, parse_snapshot, stored_units


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


def _show_outcome(outcome) -> None:
    print(
        f"Import run {outcome.run_id}: imported {outcome.imported}, "
        f"duplicates {outcome.duplicates}, skipped {outcome.skipped}."
    )


def cmd_import(args: argparse.Namespace) -> int:
    try:
        with session_scope(make_engine()) as session:
            if args.source == "asb-gmail":
                if args.file is not None:
                    raise ImportFailure(
                        "asb-gmail reads its configured label and does not take a file."
                    )
                settings = get_gmail_settings()
                settings.require("label")
                outcome = import_gmail(
                    session, GmailClient(settings), settings.label or "", args.portfolio
                )
            else:
                if args.file is None:
                    raise ImportFailure(f"{args.source} needs a file to import.")
                outcome = import_file(session, args.source, Path(args.file), args.portfolio)
    except ImportFailure as exc:
        _fail(str(exc))
        return 2
    _show_outcome(outcome)
    return 0


def cmd_import_list(_: argparse.Namespace) -> int:
    with session_scope(make_engine()) as session:
        runs = session.execute(
            select(ImportRun, Portfolio.name)
            .join(Portfolio, ImportRun.portfolio_id == Portfolio.id)
            .order_by(ImportRun.id)
        ).all()
        if not runs:
            print("No imports yet.")
        for run, portfolio_name in runs:
            finished = run.finished_at.isoformat(timespec="seconds") if run.finished_at else "-"
            print(
                f"{run.id}\t{run.status}\t{run.source}\t{portfolio_name}\t"
                f"{run.rows_imported}\t{finished}\t{run.filename or '-'}"
            )
    return 0


def cmd_import_undo(args: argparse.Namespace) -> int:
    try:
        with session_scope(make_engine()) as session:
            removed = undo_import(session, args.id)
    except ImportFailure as exc:
        _fail(str(exc))
        return 2
    print(f"Undid import run {args.id}; removed {removed} row(s).")
    return 0


def _manual_quantity(value: str, kind: TradeType) -> Decimal:
    quantity = parse_decimal(value, "Quantity")
    if not quantity or (kind != TradeType.ADJUSTMENT and quantity < 0):
        raise RowProblem("Quantity must be positive (or signed for an adjustment).")
    exponent = quantity.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -8:
        raise RowProblem("Quantity has more than 8 decimal places.")
    return quantity


def cmd_trade_add(args: argparse.Namespace) -> int:
    try:
        kind = TradeType(args.type)
        exchange, code, currency = listing(args.exchange, args.code, args.currency)
        trade_date = parse_date(args.date, "Trade date")
        quantity = _manual_quantity(args.quantity, kind)
        price = parse_price(args.price, "Price")
        fees = parse_money(args.fees, "Fees")
    except (RowProblem, ValueError) as exc:
        _fail(str(exc))
        return 2

    try:
        with session_scope(make_engine()) as session:
            portfolio = portfolio_named(session, args.portfolio, lock=True)
            instrument = session.scalar(
                select(Instrument).where(Instrument.exchange == exchange, Instrument.code == code)
            )
            if instrument is None:
                instrument = Instrument(
                    exchange=exchange,
                    code=code,
                    currency=currency,
                    name=args.name or code,
                )
                session.add(instrument)
                session.flush()
            elif instrument.currency != currency:
                raise ImportFailure(
                    f"{code} on {exchange} is already stored in {instrument.currency}, "
                    f"not {currency}."
                )
            trade = Trade(
                portfolio_id=portfolio.id,
                instrument_id=instrument.id,
                trade_date=trade_date,
                trade_type=kind.value,
                quantity=quantity,
                price=price,
                currency=currency,
                fees=fees,
                source="manual",
                external_ref=f"manual:{uuid.uuid4().hex}",
                notes=args.notes,
            )
            session.add(trade)
            session.flush()
            trade_id = trade.id
    except ImportFailure as exc:
        _fail(str(exc))
        return 2
    print(f"Added manual trade {trade_id}: {kind.value} {quantity} {exchange}:{code}.")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    try:
        snapshot = parse_snapshot(read_input(Path(args.file)))
        with session_scope(make_engine()) as session:
            portfolio = portfolio_named(session, args.portfolio)
            actual = stored_units(session, portfolio.id)
    except (ImportFailure, FileProblem) as exc:
        _fail(str(exc))
        return 2
    differences = compare(actual, snapshot)
    if not differences:
        print(
            f"OK: {args.source} snapshot matches {args.portfolio!r} ({len(snapshot)} holding(s))."
        )
        return 0
    print("MISMATCH\tAPP UNITS\tSNAPSHOT UNITS\tDIFFERENCE")
    for item in differences:
        print(
            f"{item.exchange}:{item.code}\t{item.app_units}\t{item.snapshot_units}\t{item.variance}"
        )
    _fail(f"{len(differences)} holding(s) do not match the {args.source} snapshot.")
    return 1


def cmd_gmail_authorise(args: argparse.Namespace) -> int:
    authorise(get_gmail_settings(), Path(args.output))
    print(f"Saved the read-only refresh token to {args.output}. Keep this file private.")
    return 0


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

    importing = sub.add_parser("import", help="Import broker trades or manage import runs")
    isub = importing.add_subparsers(dest="source", required=True)
    for name, importer in FILE_IMPORTERS.items():
        command = isub.add_parser(name, help=importer.summary)
        command.add_argument("file")
        command.add_argument("--portfolio", required=True)
        command.set_defaults(func=cmd_import)
    gmail_import = isub.add_parser("asb-gmail", help=AsbGmailImporter.summary)
    gmail_import.add_argument("--portfolio", required=True)
    gmail_import.set_defaults(func=cmd_import, file=None)
    isub.add_parser("list", help="List import runs").set_defaults(func=cmd_import_list)
    undo = isub.add_parser("undo", help="Undo a successful import")
    undo.add_argument("id", type=int)
    undo.set_defaults(func=cmd_import_undo)

    trade = sub.add_parser("trade", help="Manage manually entered trades")
    tsub = trade.add_subparsers(dest="trade_command", required=True)
    add_trade = tsub.add_parser("add", help="Add a correction, gift or opening balance")
    add_trade.add_argument("--portfolio", required=True)
    add_trade.add_argument("--date", required=True)
    add_trade.add_argument("--type", required=True, choices=[kind.value for kind in TradeType])
    add_trade.add_argument("--exchange", required=True)
    add_trade.add_argument("--code", required=True)
    add_trade.add_argument("--quantity", required=True)
    add_trade.add_argument("--price", default="0")
    add_trade.add_argument("--currency")
    add_trade.add_argument("--fees", default="0")
    add_trade.add_argument("--name")
    add_trade.add_argument("--notes")
    add_trade.set_defaults(func=cmd_trade_add)

    reconcile = sub.add_parser("reconcile", help="Compare holdings with a snapshot CSV")
    reconcile.add_argument("source", help="Broker or registry name shown in the report")
    reconcile.add_argument("file")
    reconcile.add_argument("--portfolio", required=True)
    reconcile.set_defaults(func=cmd_reconcile)

    gmail = sub.add_parser("gmail", help="Set up read-only Gmail access")
    gsub = gmail.add_subparsers(dest="gmail_command", required=True)
    auth = gsub.add_parser("authorise", help="Create a read-only Gmail refresh token")
    auth.add_argument("--output", default=".env.gmail")
    auth.set_defaults(func=cmd_gmail_authorise)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        _fail(str(exc))
    except GmailError as exc:
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
