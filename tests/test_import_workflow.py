from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_digest.cli import main
from portfolio_digest.models import ImportRun, ImportStatus, Instrument, Trade

FIXTURES = Path(__file__).parent / "fixtures"


def test_full_history_import_is_repeatable_reconciles_and_can_be_undone(engine, capsys):
    assert main(["portfolio", "add", "Made-up Portfolio"]) == 0
    source = str(FIXTURES / "sharesies-transactions.csv")

    assert main(["import", "sharesies", source, "--portfolio", "Made-up Portfolio"]) == 0
    assert "imported 5, duplicates 0, skipped 1" in capsys.readouterr().out

    assert main(["import", "sharesies", source, "--portfolio", "Made-up Portfolio"]) == 0
    assert "imported 0, duplicates 5" in capsys.readouterr().out
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 5

    snapshot = str(FIXTURES / "reconcile-match.csv")
    assert main(["reconcile", "sharesies", snapshot, "--portfolio", "Made-up Portfolio"]) == 0
    assert "OK:" in capsys.readouterr().out

    assert main(["import", "undo", "1"]) == 0
    assert "removed 0" in capsys.readouterr().out
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 5
        assert set(session.scalars(select(Trade.import_run_id))) == {2}
    assert main(["reconcile", "sharesies", snapshot, "--portfolio", "Made-up Portfolio"]) == 0
    assert main(["import", "undo", "2"]) == 0
    assert "removed 5" in capsys.readouterr().out
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 0
        assert session.get(ImportRun, 1).status == ImportStatus.UNDONE.value


def test_malformed_import_is_logged_and_writes_no_partial_history(engine, tmp_path, capsys):
    assert main(["portfolio", "add", "Personal"]) == 0
    malformed = tmp_path / "made-up-malformed.csv"
    malformed.write_bytes(
        (FIXTURES / "sharesies-transactions.csv").read_bytes()
        + b"bad,31/02/2024,AIR,NZX,one,7,BUY,,0,NZD,7,Order\n"
    )

    assert main(["import", "sharesies", str(malformed), "--portfolio", "Personal"]) == 2
    assert "No trades were imported" in capsys.readouterr().err
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 0
        run = session.scalar(select(ImportRun))
        assert run.status == ImportStatus.FAILED.value
        assert "one" not in (run.notes or "")


def test_manual_adjustment_changes_reconciliation_and_mismatch_is_visible(engine, capsys):
    assert main(["portfolio", "add", "Personal"]) == 0
    assert (
        main(
            [
                "trade",
                "add",
                "--portfolio",
                "Personal",
                "--date",
                "2024-01-01",
                "--type",
                "adjustment",
                "--exchange",
                "NZX",
                "--code",
                "AIR",
                "--quantity",
                "10.5",
                "--notes",
                "Made-up gifted shares",
            ]
        )
        == 0
    )
    capsys.readouterr()

    snapshot = FIXTURES / "reconcile-match.csv"
    assert main(["reconcile", "registry", str(snapshot), "--portfolio", "Personal"]) == 1
    output = capsys.readouterr()
    assert "NZX:AIR\t10.50000000\t10.34567890\t0.15432110" in output.out
    assert "holding(s) do not match" in output.err


def test_gmail_cli_builds_importer_and_imports_with_fake_client(engine, monkeypatch, capsys):
    from portfolio_digest import cli
    from portfolio_digest.config import GmailSettings

    class FakeClient:
        def __init__(self, settings):
            assert settings.label == "Made-up ASB label"

        def label_id(self, label):
            assert label == "Made-up ASB label"
            return "made-up-label-id"

        def message_ids(self, label_id):
            assert label_id == "made-up-label-id"
            return iter(["unreadable", "made-up-message"])

        def raw_message(self, message_id):
            original = (FIXTURES / "asb-contract-note.eml").read_bytes()
            if message_id == "unreadable":
                return original.replace(b"Quantity: 100\n", b"")
            assert message_id == "made-up-message"
            return original

    monkeypatch.setattr(cli, "GmailClient", FakeClient)
    monkeypatch.setattr(cli, "get_gmail_settings", lambda: GmailSettings(label="Made-up ASB label"))
    assert main(["portfolio", "add", "Personal"]) == 0
    for expected in ("imported 1, duplicates 0", "imported 0, duplicates 1"):
        assert main(["import", "asb-gmail", "--portfolio", "Personal"]) == 0
        output = capsys.readouterr().out
        assert expected + ", skipped 1" in output
        assert "Skipped message unreadable: contract note is missing: quantity" in output
    with Session(engine) as session:
        trade = session.scalars(select(Trade)).one()
        assert trade.external_ref == "contract:40012345"
        assert trade.source == "asb_securities"


@pytest.mark.parametrize(
    "second_row",
    [
        b"15/03/2016 30001234 Sell AIR NZX 10 1.0000 1.00 10.00\n",
    ],
)
def test_repeated_contract_reference_in_one_file_is_a_row_error(
    engine, tmp_path, capsys, second_row
):
    assert main(["portfolio", "add", "Personal"]) == 0
    path = tmp_path / "made-up-repeated-statement.txt"
    path.write_bytes(b"14/03/2016 30001234 Buy FPH NZX 100 9.8500 30.00 985.00\n" + second_row)
    assert main(["import", "asb-statement", str(path), "--portfolio", "Personal"]) == 2
    error = capsys.readouterr().err
    assert "line 2" in error and "repeated (first at line 1)" in error
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 0
        assert session.scalars(select(ImportRun)).one().status == "failed"


def test_conflicting_stored_contract_reference_fails_without_partial_writes(
    engine, tmp_path, capsys
):
    assert main(["portfolio", "add", "Personal"]) == 0
    path = tmp_path / "made-up-statement.txt"
    path.write_text("14/03/2016 30001234 Buy FPH NZX 100 9.8500 30.00 985.00\n")
    assert main(["import", "asb-statement", str(path), "--portfolio", "Personal"]) == 0
    path.write_text(
        "15/03/2016 30001235 Buy AIR NZX 10 1.0000 1.00 10.00\n"
        "15/03/2016 30001234 Sell AIR NZX 10 1.0000 1.00 10.00\n"
    )
    assert main(["import", "asb-statement", str(path), "--portfolio", "Personal"]) == 2
    assert "line 2: Reference 'contract:30001234' already exists with different" in (
        capsys.readouterr().err
    )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 1
        assert session.get(ImportRun, 2).status == "failed"


@pytest.mark.parametrize("first_two", [(2, 1), (1, 2)])
def test_undo_retains_trades_until_all_duplicate_runs_are_undone(engine, first_two):
    assert main(["portfolio", "add", "Personal"]) == 0
    for _ in range(3):
        assert (
            main(
                [
                    "import",
                    "asb-email",
                    str(FIXTURES / "asb-contract-note.eml"),
                    "--portfolio",
                    "Personal",
                ]
            )
            == 0
        )
    for run_id in first_two:
        assert main(["import", "undo", str(run_id)]) == 0
    with Session(engine) as session:
        assert session.scalars(select(Trade.import_run_id)).one() == 3
    assert main(["import", "undo", "3"]) == 0
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 0


@pytest.mark.parametrize(
    "replacement",
    [
        "15/03/2016 30001234 Buy FPH NZX 100 9.8500 30.00 985.00",
        "14/03/2016 30001234 Sell FPH NZX 100 9.8500 30.00 985.00",
        "14/03/2016 30001234 Buy AIR NZX 100 9.8500 30.00 985.00",
        "14/03/2016 30001234 Buy FPH ASX 100 9.8500 30.00 985.00",
        "14/03/2016 30001234 Buy FPH NZX 200 9.8500 30.00 1970.00",
        "14/03/2016 30001234 Buy FPH NZX 100 10.0000 30.00 1000.00",
        "14/03/2016 30001234 Buy FPH NZX 100 9.8500 31.00 985.00",
    ],
)
def test_stored_reference_requires_matching_trade_fields(engine, tmp_path, capsys, replacement):
    assert main(["portfolio", "add", "Personal"]) == 0
    path = tmp_path / "made-up-conflict.txt"
    path.write_text("14/03/2016 30001234 Buy FPH NZX 100 9.8500 30.00 985.00\n")
    args = ["import", "asb-statement", str(path), "--portfolio", "Personal"]
    assert main(args) == 0
    path.write_text(replacement + "\n")
    assert main(args) == 2
    assert "different trade fields" in capsys.readouterr().err
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 1
        assert session.get(ImportRun, 2).status == "failed"


def test_identical_repeated_references_collapse_and_undo(engine, tmp_path, capsys):
    assert main(["portfolio", "add", "Personal"]) == 0
    path = tmp_path / "made-up-repeated.txt"
    path.write_text("14/03/2016 30001234 Buy FPH NZX 100 9.8500 30.00 985.00\n" * 2)
    args = ["import", "asb-statement", str(path), "--portfolio", "Personal"]
    for expected in ("imported 1, duplicates 1", "imported 0, duplicates 2"):
        assert main(args) == 0
        assert expected in capsys.readouterr().out
    assert main(["import", "undo", "1"]) == 0
    with Session(engine) as session:
        assert session.scalars(select(Trade.import_run_id)).one() == 2
    assert main(["import", "undo", "2"]) == 0
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 0


@pytest.mark.parametrize("stored_first", [False, True])
def test_gmail_resends_and_amendments_do_not_block_other_trades(
    engine, monkeypatch, capsys, stored_first
):
    from portfolio_digest import cli
    from portfolio_digest.config import GmailSettings

    original = (FIXTURES / "asb-contract-note.eml").read_bytes()
    messages = {
        "original": original,
        "resent": original,
        "amended": original.replace(b"Quantity: 100", b"Quantity: 200").replace(
            b"2,750.00", b"5,500.00"
        ),
        "new": original.replace(b"40012345", b"40012346"),
    }

    class FakeClient:
        def __init__(self, settings):
            pass

        def label_id(self, label):
            return "made-up-label"

        def message_ids(self, label_id):
            return iter(messages)

        def raw_message(self, message_id):
            return messages[message_id]

    monkeypatch.setattr(cli, "GmailClient", FakeClient)
    monkeypatch.setattr(cli, "get_gmail_settings", lambda: GmailSettings(label="Made-up label"))
    assert main(["portfolio", "add", "Personal"]) == 0
    if stored_first:
        assert (
            main(
                [
                    "import",
                    "asb-email",
                    str(FIXTURES / "asb-contract-note.eml"),
                    "--portfolio",
                    "Personal",
                ]
            )
            == 0
        )
    for imported, duplicates in ((1 if stored_first else 2, 2 if stored_first else 1), (0, 3)):
        assert main(["import", "asb-gmail", "--portfolio", "Personal"]) == 0
        output = capsys.readouterr().out
        assert f"imported {imported}, duplicates {duplicates}, skipped 1" in output
        assert "Skipped message amended: Reference 'contract:40012345'" in output
        assert "different trade fields" in output
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Trade)) == 2
        assert all(trade.quantity == 100 for trade in session.scalars(select(Trade)))
        assert all(run.status == "succeeded" for run in session.scalars(select(ImportRun)))


@pytest.mark.parametrize("gst_column", [True, False])
@pytest.mark.parametrize("email_first", [True, False])
def test_asb_statement_and_email_share_fee_basis(engine, tmp_path, capsys, gst_column, email_first):
    from decimal import Decimal

    assert main(["portfolio", "add", "Personal"]) == 0
    statement = tmp_path / "made-up-statement.txt"
    fees = "30.00 4.50" if gst_column else "34.50"
    statement.write_text(f"14/03/2024 40012345 Buy FPH NZX 100 27.5000 {fees} 2750.00\n")
    inputs = [("asb-email", FIXTURES / "asb-contract-note.eml"), ("asb-statement", statement)]
    if not email_first:
        inputs.reverse()
    for source, path in inputs:
        assert main(["import", source, str(path), "--portfolio", "Personal"]) == 0
    assert "imported 0, duplicates 1" in capsys.readouterr().out
    with Session(engine) as session:
        assert session.scalars(select(Trade)).one().fees == Decimal("34.50")


@pytest.mark.parametrize("phase", ["file_parse", "gmail_fetch", "file_store", "gmail_store"])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_unexpected_import_exception_marks_run_failed(engine, monkeypatch, capsys, phase, failure):
    from portfolio_digest import cli, importing
    from portfolio_digest.config import GmailSettings

    def broken(*args, **kwargs):
        if phase.endswith("store"):
            args[0].add(Instrument(exchange="NZX", code="AIR", name="Made-up", currency="NZD"))
            args[0].flush()
        raise failure("made-up private contents must not appear")

    gmail = phase.startswith("gmail")
    if phase.endswith("store"):
        monkeypatch.setattr(importing, "_store_result", broken)
    elif gmail:
        monkeypatch.setattr(cli.AsbGmailImporter, "fetch", broken)
    else:
        monkeypatch.setattr(importing.FILE_IMPORTERS["sharesies"], "parse", broken)
    if gmail:
        monkeypatch.setattr(cli, "GmailClient", lambda settings: None)
        monkeypatch.setattr(cli, "get_gmail_settings", lambda: GmailSettings(label="Made-up"))
        if phase.endswith("store"):
            from portfolio_digest.importers.base import ParseResult

            monkeypatch.setattr(cli.AsbGmailImporter, "fetch", lambda self: ParseResult())
    assert main(["portfolio", "add", "Personal"]) == 0
    args = (
        ["import", "asb-gmail"]
        if gmail
        else ["import", "sharesies", str(FIXTURES / "sharesies-transactions.csv")]
    )
    if failure is RuntimeError:
        assert main([*args, "--portfolio", "Personal"]) == 2
        error = capsys.readouterr().err
        assert "failed unexpectedly" in error
    else:
        with pytest.raises(failure, match="made-up private contents"):
            main([*args, "--portfolio", "Personal"])
        error = capsys.readouterr().err
    assert "private contents" not in error
    with Session(engine) as session:
        run = session.scalars(select(ImportRun)).one()
        assert run.status == "failed"
        assert run.finished_at is not None
        assert "private contents" not in run.notes
        assert session.scalar(select(func.count()).select_from(Trade)) == 0
        assert session.scalar(select(func.count()).select_from(Instrument)) == 0


@pytest.mark.parametrize(
    "old,new",
    [
        (b"260.50", b"1e30"),
        (b"0.6125", b"1e-25"),
        (b",1.25,", b",1e20,"),
        (b",5.00,", b",100000000000000,"),
        (b",5.00,", b",1e25,"),
    ],
)
def test_out_of_range_decimal_is_a_logged_row_error(engine, tmp_path, capsys, old, new):
    assert main(["portfolio", "add", "Personal"]) == 0
    path = tmp_path / "made-up-range.csv"
    path.write_bytes((FIXTURES / "sharesight-trades.csv").read_bytes().replace(old, new))
    assert main(["import", "sharesight-csv", str(path), "--portfolio", "Personal"]) == 2
    assert "line 2" in capsys.readouterr().err
    with Session(engine) as session:
        assert session.scalars(select(ImportRun)).one().status == "failed"
        assert session.scalar(select(func.count()).select_from(Trade)) == 0


def test_manual_negative_fees_are_refused(capsys):
    assert (
        main(
            [
                "trade",
                "add",
                "--portfolio",
                "Made-up",
                "--date",
                "2024-01-01",
                "--type",
                "buy",
                "--exchange",
                "NZX",
                "--code",
                "AIR",
                "--quantity",
                "1",
                "--fees",
                "-5",
            ]
        )
        == 2
    )
    assert "Fees cannot be negative" in capsys.readouterr().err
