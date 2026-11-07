"""scripts/backup.sh, run with a stand-in pg_dump so no database is needed."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "backup.sh"
DAY = 86400
SH = shutil.which("sh")

pytestmark = [
    pytest.mark.posix,
    pytest.mark.skipif(os.name == "nt" or SH is None, reason="needs POSIX"),
]


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """A pg_dump that writes a small file, or fails when FAKE_PG_DUMP_FAIL is set."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pg_dump = bin_dir / "pg_dump"
    pg_dump.write_text(
        "#!/bin/sh\n"
        '[ -n "${FAKE_PG_DUMP_FAIL:-}" ] && { echo "connection refused" >&2; exit 1; }\n'
        'for arg; do case "$arg" in --file=*) echo dump > "${arg#--file=}" ;; esac; done\n'
    )
    pg_dump.chmod(0o755)
    return bin_dir


@pytest.fixture
def dump_dir(tmp_path: Path) -> Path:
    path = tmp_path / "backups"
    path.mkdir()
    return path


def run(fake_bin: Path, dump_dir: Path, **env: str) -> subprocess.CompletedProcess[str]:
    # Runs our own script with a fixed argument list.
    return subprocess.run(  # noqa: S603
        [SH, str(SCRIPT), "--once"],
        env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "DUMP_DIR": str(dump_dir),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def old_file(directory: Path, name: str, age_days: float) -> Path:
    path = directory / name
    path.write_text("old")
    when = time.time() - age_days * DAY
    os.utime(path, (when, when))
    return path


def dumps(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_writes_a_private_dump(fake_bin, dump_dir):
    result = run(fake_bin, dump_dir)
    assert result.returncode == 0, result.stderr
    [name] = dumps(dump_dir)
    assert name.startswith("portfolio-") and name.endswith(".dump")
    assert (dump_dir / name).stat().st_mode & 0o077 == 0


def test_prunes_only_dumps_older_than_the_retention(fake_bin, dump_dir):
    expired = old_file(dump_dir, "portfolio-20260101T000000Z.dump", age_days=3.1)
    kept = old_file(dump_dir, "portfolio-20260102T000000Z.dump", age_days=2.9)
    unrelated = old_file(dump_dir, "notes.txt", age_days=30)
    result = run(fake_bin, dump_dir, BACKUP_KEEP_DAYS="3")
    assert result.returncode == 0, result.stderr
    assert not expired.exists()
    assert kept.exists() and unrelated.exists()
    assert len(dumps(dump_dir)) == 3


def test_a_failed_dump_keeps_every_old_backup(fake_bin, dump_dir):
    old = old_file(dump_dir, "portfolio-20260101T000000Z.dump", age_days=30)
    result = run(fake_bin, dump_dir, FAKE_PG_DUMP_FAIL="1")
    assert result.returncode != 0
    assert "FAILED" in result.stderr
    assert dumps(dump_dir) == [old.name]


def test_clears_up_stale_partial_dumps(fake_bin, dump_dir):
    stale = old_file(dump_dir, "portfolio-20260101T000000Z.dump.partial", age_days=2)
    result = run(fake_bin, dump_dir)
    assert result.returncode == 0, result.stderr
    assert not stale.exists()


@pytest.mark.parametrize("keep", ["0", "014", "two", "-1", "14d"])
def test_refuses_a_bad_retention_before_dumping(fake_bin, dump_dir, keep):
    result = run(fake_bin, dump_dir, BACKUP_KEEP_DAYS=keep)
    assert result.returncode != 0
    assert "BACKUP_KEEP_DAYS" in result.stderr
    assert dumps(dump_dir) == []


def test_refuses_a_missing_backup_dir(fake_bin, tmp_path):
    result = run(fake_bin, tmp_path / "missing")
    assert result.returncode != 0
    assert "cannot write" in result.stderr


def install_wget(fake_bin: Path) -> None:
    """A wget that records its arguments and can echo them, as a noisy real wget might."""
    wget = fake_bin / "wget"
    wget.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$WGET_LOG"\n'
        'printf "%s\\n" "$@"\n'
        'printf "%s\\n" "$@" >&2\n'
        '[ -n "${WGET_FAIL:-}" ] && exit 1\n'
        "exit 0\n"
    )
    wget.chmod(0o755)


def test_monitor_is_pinged_once_after_a_successful_dump(fake_bin, dump_dir, tmp_path):
    install_wget(fake_bin)
    log = tmp_path / "wget.log"
    url = "https://kuma.example.invalid/api/push/SECRETTOKEN"
    result = run(fake_bin, dump_dir, BACKUP_PUSH_URL=url, WGET_LOG=str(log))
    assert result.returncode == 0, result.stderr
    assert "monitoring ping sent" in result.stdout
    assert url not in result.stdout
    assert url not in result.stderr
    assert log.read_text().count(url) == 1


def test_monitor_is_not_pinged_after_a_failed_dump(fake_bin, dump_dir, tmp_path):
    install_wget(fake_bin)
    log = tmp_path / "wget.log"
    url = "https://kuma.example.invalid/api/push/SECRETTOKEN"
    result = run(
        fake_bin,
        dump_dir,
        FAKE_PG_DUMP_FAIL="1",
        BACKUP_PUSH_URL=url,
        WGET_LOG=str(log),
    )
    assert result.returncode != 0
    assert "FAILED" in result.stderr
    assert "monitoring ping" not in result.stdout
    assert "monitoring ping" not in result.stderr
    assert url not in result.stdout
    assert url not in result.stderr
    assert not log.exists()


def test_a_failed_monitor_ping_does_not_fail_the_dump_or_print_the_url(
    fake_bin, dump_dir, tmp_path
):
    install_wget(fake_bin)
    log = tmp_path / "wget.log"
    url = "https://kuma.example.invalid/api/push/SECRETTOKEN"
    result = run(
        fake_bin,
        dump_dir,
        BACKUP_PUSH_URL=url,
        WGET_LOG=str(log),
        WGET_FAIL="1",
    )
    assert result.returncode == 0, result.stderr
    assert "monitoring ping failed" in result.stderr
    assert url not in result.stdout
    assert url not in result.stderr
    assert log.read_text().count(url) == 1
