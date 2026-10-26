"""Persist parsed broker trades as auditable, repeatable import runs."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from portfolio_digest.gmail import AsbGmailImporter, GmailError
from portfolio_digest.importers.asb import AsbEmailImporter, AsbStatementImporter
from portfolio_digest.importers.base import (
    FileProblem,
    Importer,
    ParseContext,
    ParseResult,
    RowError,
)
from portfolio_digest.importers.sharesies import SharesiesImporter
from portfolio_digest.importers.sharesight_csv import SharesightCsvImporter
from portfolio_digest.models import (
    ImportRun,
    ImportStatus,
    ImportTradeMatch,
    Income,
    Instrument,
    Portfolio,
    Trade,
)

FILE_IMPORTERS: dict[str, Importer] = {
    importer.name: importer
    for importer in (
        SharesiesImporter(),
        AsbEmailImporter(),
        AsbStatementImporter(),
        SharesightCsvImporter(),
    )
}


class ImportFailure(Exception):
    """An import cannot safely continue. The message is suitable for the CLI."""


@dataclass(frozen=True)
class ImportOutcome:
    run_id: int
    imported: int
    duplicates: int
    skipped: int


def portfolio_named(session: Session, name: str, *, lock: bool = False) -> Portfolio:
    statement = select(Portfolio).where(func.lower(Portfolio.name) == name.lower())
    if lock:
        statement = statement.with_for_update()
    portfolio = session.scalar(statement)
    if portfolio is None:
        raise ImportFailure(
            f"There is no portfolio called {name!r}. Add it with `portfolio-digest portfolio add`."
        )
    return portfolio


def read_input(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ImportFailure(f"Cannot read {path}: {exc.strerror or type(exc).__name__}.") from None


def _new_run(
    session: Session,
    portfolio: Portfolio,
    source: str,
    filename: str | None,
    content_sha256: str | None,
) -> ImportRun:
    run = ImportRun(
        portfolio_id=portfolio.id,
        source=source,
        filename=filename,
        content_sha256=content_sha256,
        status=ImportStatus.RUNNING.value,
    )
    session.add(run)
    session.flush()
    # Keep the audit row even if parsing or persistence fails afterwards.
    session.commit()
    return run


def _fail_run(session: Session, run: ImportRun, notes: str) -> None:
    run.status = ImportStatus.FAILED.value
    run.finished_at = datetime.now(UTC)
    run.notes = notes
    session.commit()


def _mark_storage_failed(session: Session, run_id: int) -> None:
    session.rollback()
    run = session.get(ImportRun, run_id)
    if run is not None and run.status == ImportStatus.RUNNING.value:
        _fail_run(session, run, "Parsed trades could not be stored; no trades imported.")


def find_or_create_instrument(
    session: Session, exchange: str, code: str, currency: str, name: str | None = None
) -> Instrument:
    """Return one canonical listing, creating it when this is its first trade."""
    instrument = session.scalar(
        select(Instrument).where(Instrument.exchange == exchange, Instrument.code == code)
    )
    if instrument is None:
        instrument = Instrument(
            exchange=exchange,
            code=code,
            name=name or code,
            currency=currency,
        )
        session.add(instrument)
        session.flush()
    elif instrument.currency != currency:
        raise ImportFailure(
            f"{code} on {exchange} is already stored in {instrument.currency}, not {currency}."
        )
    return instrument


def _store_result(
    session: Session,
    portfolio: Portfolio,
    run: ImportRun,
    trade_source: str,
    result: ParseResult,
) -> ImportOutcome:
    references = [trade.external_ref for trade in result.trades]
    existing = (
        {
            trade.external_ref: (trade, instrument)
            for trade, instrument in session.execute(
                select(Trade, Instrument)
                .join(Instrument, Trade.instrument_id == Instrument.id)
                .where(
                    Trade.portfolio_id == portfolio.id,
                    Trade.source == trade_source,
                    Trade.external_ref.in_(references),
                )
            )
        }
        if references
        else {}
    )
    seen: dict[str, str] = {}
    for parsed in result.trades:
        if parsed.external_ref in seen:
            result.errors.append(
                RowError(
                    parsed.where,
                    f"Reference {parsed.external_ref!r} is repeated (first at "
                    f"{seen[parsed.external_ref]}). Each row must have a unique reference.",
                )
            )
        seen.setdefault(parsed.external_ref, parsed.where)
        match = existing.get(parsed.external_ref)
        if match is not None:
            trade, instrument = match
            stored = (
                trade.trade_date,
                trade.trade_type,
                instrument.exchange,
                instrument.code,
                trade.quantity,
                trade.price,
                trade.currency,
                trade.fees,
                trade.fx_rate,
            )
            incoming = (
                parsed.trade_date,
                parsed.trade_type.value,
                parsed.exchange,
                parsed.code,
                parsed.quantity,
                parsed.price,
                parsed.currency,
                parsed.fees,
                parsed.fx_rate,
            )
            if stored != incoming:
                result.errors.append(
                    RowError(
                        parsed.where,
                        f"Reference {parsed.external_ref!r} already exists "
                        "with different trade fields.",
                    )
                )

    if result.errors:
        _fail_run(
            session,
            run,
            f"{len(result.errors)} row error(s); no trades imported.",
        )
        details = "\n".join(str(error) for error in result.errors)
        raise ImportFailure(
            f"Import run {run.id} failed with {len(result.errors)} row error(s). "
            f"No trades were imported.\n{details}"
        )

    instruments: dict[tuple[str, str], Instrument] = {}
    imported = 0
    for parsed in result.trades:
        if parsed.external_ref in existing:
            trade, _ = existing[parsed.external_ref]
            session.add(ImportTradeMatch(import_run_id=run.id, trade_id=trade.id))
            continue
        key = (parsed.exchange, parsed.code)
        instrument = instruments.get(key)
        if instrument is None:
            instrument = find_or_create_instrument(
                session,
                parsed.exchange,
                parsed.code,
                parsed.currency,
                parsed.name,
            )
            instruments[key] = instrument
        session.add(
            Trade(
                portfolio_id=portfolio.id,
                instrument_id=instrument.id,
                trade_date=parsed.trade_date,
                trade_type=parsed.trade_type.value,
                quantity=parsed.quantity,
                price=parsed.price,
                currency=parsed.currency,
                fees=parsed.fees,
                fx_rate=parsed.fx_rate,
                source=trade_source,
                external_ref=parsed.external_ref,
                import_run_id=run.id,
                notes=parsed.notes,
            )
        )
        imported += 1

    run.status = ImportStatus.SUCCEEDED.value
    run.rows_imported = imported
    run.finished_at = datetime.now(UTC)
    duplicates = len(result.trades) - imported
    run.notes = f"{duplicates} duplicate(s), {len(result.skipped)} non-trade row(s) skipped."
    session.commit()
    return ImportOutcome(run.id, imported, duplicates, len(result.skipped))


def import_file(session: Session, source: str, path: Path, portfolio_name: str) -> ImportOutcome:
    importer = FILE_IMPORTERS.get(source)
    if importer is None:
        choices = ", ".join(FILE_IMPORTERS)
        raise ImportFailure(f"Unknown import source {source!r}. Choose one of: {choices}.")
    portfolio = portfolio_named(session, portfolio_name, lock=True)
    try:
        content = read_input(path)
    except ImportFailure:
        run = _new_run(session, portfolio, importer.name, path.name[:255], None)
        _fail_run(session, run, "File could not be read; no trades imported.")
        raise
    run = _new_run(
        session,
        portfolio,
        importer.name,
        path.name[:255],
        hashlib.sha256(content).hexdigest(),
    )
    try:
        result = importer.parse(content, ParseContext(portfolio.base_currency))
    except FileProblem as exc:
        _fail_run(session, run, "File could not be parsed; no trades imported.")
        raise ImportFailure(f"Import run {run.id} failed. {exc}") from None
    portfolio = session.get(Portfolio, portfolio.id, with_for_update=True)
    assert portfolio is not None  # protected from deletion by the import run foreign key
    run_id = run.id
    try:
        return _store_result(session, portfolio, run, importer.trade_source, result)
    except (ImportFailure, SQLAlchemyError):
        _mark_storage_failed(session, run_id)
        raise


def import_gmail(
    session: Session, importer: AsbGmailImporter, portfolio_name: str
) -> ImportOutcome:
    portfolio = portfolio_named(session, portfolio_name, lock=True)
    run = _new_run(session, portfolio, importer.name, None, None)
    try:
        result = importer.fetch()
    except GmailError:
        _fail_run(session, run, "Gmail could not be read; no trades imported.")
        raise
    portfolio = session.get(Portfolio, portfolio.id, with_for_update=True)
    assert portfolio is not None
    run_id = run.id
    try:
        return _store_result(session, portfolio, run, importer.trade_source, result)
    except (ImportFailure, SQLAlchemyError):
        _mark_storage_failed(session, run_id)
        raise


def undo_import(session: Session, run_id: int) -> int:
    run = session.get(ImportRun, run_id)
    if run is None:
        raise ImportFailure(f"There is no import run {run_id}.")
    # Serialise undo with imports and other undos in this portfolio.
    session.get(Portfolio, run.portfolio_id, with_for_update=True)
    session.refresh(run, with_for_update=True)
    if run.status != ImportStatus.SUCCEEDED.value:
        raise ImportFailure(f"Import run {run_id} is {run.status}, so it cannot be undone.")
    for trade in session.scalars(select(Trade).where(Trade.import_run_id == run.id)):
        replacement = session.scalar(
            select(ImportTradeMatch.import_run_id)
            .join(ImportRun, ImportTradeMatch.import_run_id == ImportRun.id)
            .where(
                ImportTradeMatch.trade_id == trade.id,
                ImportRun.id != run.id,
                ImportRun.status == ImportStatus.SUCCEEDED.value,
            )
            .order_by(ImportRun.id)
            .limit(1)
        )
        if replacement is not None:
            trade.import_run_id = replacement
    session.flush()
    trade_count = session.execute(delete(Trade).where(Trade.import_run_id == run.id)).rowcount
    income_count = session.execute(delete(Income).where(Income.import_run_id == run.id)).rowcount
    run.status = ImportStatus.UNDONE.value
    run.finished_at = datetime.now(UTC)
    session.commit()
    return (trade_count or 0) + (income_count or 0)
