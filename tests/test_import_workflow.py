from pathlib import Path

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
