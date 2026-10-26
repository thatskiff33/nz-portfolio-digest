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
from portfolio_digest.importers.base import FileProblem, Importer, ParseContext, ParseResult
from portfolio_digest.importers.sharesies import SharesiesImporter
from portfolio_digest.importers.sharesight_csv import SharesightCsvImporter
from portfolio_digest.models import (
    ImportRun,
    ImportStatus,
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


def _finish_failed(session: Session, run: ImportRun, result: ParseResult) -> None:
    run.status = ImportStatus.FAILED.value
    run.finished_at = datetime.now(UTC)
    run.notes = f"{len(result.errors)} row error(s); no trades imported."
    session.commit()


def _mark_storage_failed(session: Session, run_id: int) -> None:
    session.rollback()
    run = session.get(ImportRun, run_id)
    if run is not None and run.status == ImportStatus.RUNNING.value:
        run.status = ImportStatus.FAILED.value
        run.finished_at = datetime.now(UTC)
        run.notes = "Parsed trades could not be stored; no trades imported."
        session.commit()


def _store_result(
    session: Session,
    portfolio: Portfolio,
    run: ImportRun,
    trade_source: str,
    result: ParseResult,
) -> ImportOutcome:
    if result.errors:
        _finish_failed(session, run, result)
        details = "\n".join(str(error) for error in result.errors)
        raise ImportFailure(
            f"Import run {run.id} failed with {len(result.errors)} row error(s). "
            f"No trades were imported.\n{details}"
        )

    references = [trade.external_ref for trade in result.trades]
    existing = (
        set(
            session.scalars(
                select(Trade.external_ref).where(
                    Trade.portfolio_id == portfolio.id,
                    Trade.source == trade_source,
                    Trade.external_ref.in_(references),
                )
            )
        )
        if references
        else set()
    )

    instruments: dict[tuple[str, str], Instrument] = {}
    imported = 0
    seen = set(existing)
    for parsed in result.trades:
        if parsed.external_ref in seen:
            continue
        seen.add(parsed.external_ref)
        key = (parsed.exchange, parsed.code)
        instrument = instruments.get(key)
        if instrument is None:
            instrument = session.scalar(
                select(Instrument).where(
                    Instrument.exchange == parsed.exchange, Instrument.code == parsed.code
                )
            )
            if instrument is None:
                instrument = Instrument(
                    exchange=parsed.exchange,
                    code=parsed.code,
                    name=parsed.name or parsed.code,
                    currency=parsed.currency,
                )
                session.add(instrument)
                session.flush()
            elif instrument.currency != parsed.currency:
                raise ImportFailure(
                    f"{parsed.code} on {parsed.exchange} is already stored in "
                    f"{instrument.currency}, not {parsed.currency}."
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
        run.status = ImportStatus.FAILED.value
        run.finished_at = datetime.now(UTC)
        run.notes = "File could not be read; no trades imported."
        session.commit()
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
        run.status = ImportStatus.FAILED.value
        run.finished_at = datetime.now(UTC)
        run.notes = "File could not be parsed; no trades imported."
        session.commit()
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
        run.status = ImportStatus.FAILED.value
        run.finished_at = datetime.now(UTC)
        run.notes = "Gmail could not be read; no trades imported."
        session.commit()
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
    run = session.get(ImportRun, run_id, with_for_update=True)
    if run is None:
        raise ImportFailure(f"There is no import run {run_id}.")
    if run.status != ImportStatus.SUCCEEDED.value:
        raise ImportFailure(f"Import run {run_id} is {run.status}, so it cannot be undone.")
    trade_count = session.execute(delete(Trade).where(Trade.import_run_id == run.id)).rowcount
    income_count = session.execute(delete(Income).where(Income.import_run_id == run.id)).rowcount
    run.status = ImportStatus.UNDONE.value
    run.finished_at = datetime.now(UTC)
    session.commit()
    return (trade_count or 0) + (income_count or 0)
