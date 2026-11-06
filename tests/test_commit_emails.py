"""The PR check covers every new commit without disclosing rejected identities."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-commit-emails.py"
GOOD = "invented@users.noreply.github.com"
BAD = "invented@example.invalid"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")


@pytest.fixture
def repo(tmp_path):
    def git(*args, **kwargs):
        return (
            subprocess.check_output(  # noqa: S603
                ["git", *args],  # noqa: S607
                cwd=tmp_path,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
            .decode()
            .strip()
        )

    git("init", "-q")

    def commit(author=GOOD, committer=GOOD):
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Invented",
            GIT_COMMITTER_NAME="Invented",
            GIT_AUTHOR_EMAIL=author,
            GIT_COMMITTER_EMAIL=committer,
        )
        git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "Invented", env=env)
        return git("rev-parse", "HEAD")

    def check(base, head):
        return subprocess.run(  # noqa: S603
            [sys.executable, str(SCRIPT), base, head],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    return commit, check


@pytest.mark.parametrize(
    "email", [GOOD, "123+invented@users.noreply.github.com", "noreply@github.com"]
)
def test_accepts_noreply_and_excludes_base_history(repo, email):
    commit, check = repo
    base = commit(BAD, BAD)
    head = commit(email, email)
    result = check(base, head)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "author,committer",
    [
        (BAD, GOOD),
        (GOOD, BAD),
        ("invented@users.noreply.github.com.evil.invalid", GOOD),
        ("@users.noreply.github.com", GOOD),
        ("other@github.com", GOOD),
    ],
)
def test_rejects_bad_identity_without_disclosing_it(repo, author, committer):
    commit, check = repo
    base = commit()
    head = commit(author, committer)
    result = check(base, head)
    assert result.returncode == 1
    assert result.stdout.strip() == f"{head} non-noreply email"
    assert author not in result.stdout + result.stderr
    assert committer not in result.stdout + result.stderr


def test_checks_intermediate_commits_and_head_even_for_empty_range(repo):
    commit, check = repo
    base = commit()
    bad = commit(BAD)
    head = commit()
    assert check(base, head).stdout.strip() == f"{bad} non-noreply email"
    assert check(bad, bad).returncode == 1


@pytest.mark.parametrize("missing_head", [False, True])
def test_missing_revision_fails_closed_without_git_error_output(repo, missing_head):
    commit, check = repo
    head = commit()
    result = check(head, "0" * 40) if missing_head else check("0" * 40, head)
    assert result.returncode != 0
    assert result.stderr.strip() == "Cannot inspect commit range."
