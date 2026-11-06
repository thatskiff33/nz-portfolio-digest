"""Private artefacts stay out of git without hiding project assets."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")


def ignored(paths):
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            f"core.excludesFile={os.devnull}",
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
        ],
        cwd=ROOT,
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1)
    return set(result.stdout.rstrip("\0").split("\0")) - {""}


def test_sensitive_files_are_ignored():
    paths = [
        "annual-portfolio-1.html",
        "preview.html",
        "out/year-end-2026.html",
        "contract-note.eml",
        "statement.txt",
        "dividend.txt",
        "snapshot.json",
        "holdings.xlsx",
        "backup.sql",
        "state/scheduler.json",
        "client_secret.json",
        "credentials.json",
        "token.json",
        "nested/preview.htm",
        "nested/export.ofx",
        "nested/export.qif",
        "nested/backup.sql.gz",
        "state/pending",
        "client_secret_example.json",
    ]
    assert ignored(paths) == set(paths)


def test_no_tracked_file_is_ignored():
    paths = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)  # noqa: S607
        .decode()
        .rstrip("\0")
        .split("\0")
    )
    assert ignored(paths) == set()
