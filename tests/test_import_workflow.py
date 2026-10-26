from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_digest.cli import main
from portfolio_digest.models import ImportRun, ImportStatus, Trade

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
            return iter(["made-up-message"])

        def raw_message(self, message_id):
            assert message_id == "made-up-message"
            return (FIXTURES / "asb-contract-note.eml").read_bytes()

    monkeypatch.setattr(cli, "GmailClient", FakeClient)
    monkeypatch.setattr(cli, "get_gmail_settings", lambda: GmailSettings(label="Made-up ASB label"))
    assert main(["portfolio", "add", "Personal"]) == 0
    for expected in ("imported 1, duplicates 0", "imported 0, duplicates 1"):
        assert main(["import", "asb-gmail", "--portfolio", "Personal"]) == 0
        assert expected in capsys.readouterr().out
    with Session(engine) as session:
        trade = session.scalars(select(Trade)).one()
        assert trade.external_ref == "contract:40012345"
        assert trade.source == "asb_securities"


@pytest.mark.parametrize(
    "second_row",
    [
        b"15/03/2016 30001234 Sell AIR NZX 10 1.0000 1.00 10.00\n",
        b"14/03/2016 30001234 Buy FPH NZX 100 9.8500 30.00 985.00\n",
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
