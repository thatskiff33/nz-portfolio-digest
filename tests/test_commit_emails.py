"""The PR checks cover every new commit and the PR text without disclosing addresses."""

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

    def commit(author=GOOD, committer=GOOD, message="Invented"):
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Invented",
            GIT_COMMITTER_NAME="Invented",
            GIT_AUTHOR_EMAIL=author,
            GIT_COMMITTER_EMAIL=committer,
        )
        git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", message, env=env)
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


def check_pr_text(title, body):
    env = dict(os.environ, PR_TITLE=title, PR_BODY=body)
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--pr-text"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


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


def trailer(email):
    return f"Invented change\n\nCo-authored-by: Invented Person <{email}>"


def test_rejects_personal_co_author_trailer_on_noreply_commit(repo):
    commit, check = repo
    base = commit()
    head = commit(message=trailer(BAD))
    result = check(base, head)
    assert result.returncode == 1
    assert result.stdout.strip() == f"{head} non-noreply email in trailer"
    assert BAD not in result.stdout + result.stderr


def test_trailer_key_is_case_insensitive(repo):
    commit, check = repo
    base = commit()
    head = commit(message=f"Invented change\n\nCo-Authored-By: Invented <{BAD}>")
    assert check(base, head).stdout.strip() == f"{head} non-noreply email in trailer"


@pytest.mark.parametrize(
    "email",
    [
        GOOD,
        "334046780+sekreton[bot]@users.noreply.github.com",
        "noreply@github.com",
        "noreply@anthropic.com",
        "cursoragent@cursor.com",
    ],
)
def test_accepts_noreply_and_allow_listed_trailers(repo, email):
    commit, check = repo
    base = commit()
    head = commit(message=trailer(email))
    result = check(base, head)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "message",
    [
        f"Invented change\n\nContact {BAD} for details.",
        f"Invented change\n\nReviewed-by: Invented <{BAD}>",
        f"Invented change mentioning {GOOD}.evil.invalid",
        f"Invented {GOOD}.evil.invalid\n\nCo-authored-by: {GOOD}",
        "Invented change\n\nContact invented@under_score.invalid",
    ],
)
def test_rejects_address_elsewhere_in_message(repo, message):
    commit, check = repo
    base = commit()
    head = commit(message=message)
    result = check(base, head)
    assert result.returncode == 1
    assert result.stdout.strip() == f"{head} non-noreply email in message"
    assert BAD not in result.stdout + result.stderr


def test_reports_identity_trailer_and_message_separately(repo):
    commit, check = repo
    base = commit()
    head = commit(BAD, message=f"Invented {BAD}\n\nCo-authored-by: Invented <{BAD}>")
    result = check(base, head)
    assert result.stdout.splitlines() == [
        f"{head} non-noreply email",
        f"{head} non-noreply email in trailer",
        f"{head} non-noreply email in message",
    ]
    assert BAD not in result.stdout + result.stderr


def test_accepts_pr_text_with_only_noreply_and_tool_addresses():
    body = f"Invented change.\n\nCo-authored-by: Invented <{GOOD}>\nnoreply@anthropic.com"
    result = check_pr_text("Invented title", body)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "title,body,expected",
    [
        (f"Invented {BAD}", "", ["pull request title non-noreply email"]),
        ("Invented", f"Co-authored-by: Invented <{BAD}>", ["pull request body non-noreply email"]),
        (
            BAD,
            BAD,
            ["pull request title non-noreply email", "pull request body non-noreply email"],
        ),
    ],
)
def test_rejects_pr_text_address_without_disclosing_it(title, body, expected):
    result = check_pr_text(title, body)
    assert result.returncode == 1
    assert result.stdout.splitlines() == expected
    assert BAD not in result.stdout + result.stderr
