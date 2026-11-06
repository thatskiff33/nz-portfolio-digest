"""Reject non-noreply addresses in PR commits and PR text without printing them.

Usage:
    check-commit-emails.py BASE_SHA HEAD_SHA
        Check the author, committer, Co-authored-by trailers and message of every commit in
        BASE..HEAD, plus HEAD itself.
    check-commit-emails.py --pr-text
        Check the PR_TITLE and PR_BODY environment variables, which GitHub may use for the
        squash commit message.

Only commit SHAs or field names are printed, never an address.
"""

import os
import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-fA-F]{40}")
MAILBOX = re.compile(r"[^\s@<>]+@users\.noreply\.github\.com")
# Deliberately broad so that anything resembling an address is inspected (fail closed).
EMAIL = re.compile(r"[^\s@<>()\"',;:]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
CO_AUTHOR = re.compile(r"co-authored-by:(.*)", re.IGNORECASE)
# Fixed co-author identities used by coding tools; none of them belongs to a person.
TOOL_IDENTITIES = frozenset(
    {
        "noreply@anthropic.com",  # Claude Code
        "cursoragent@cursor.com",  # Cursor agent
    }
)


def is_allowed(email: str) -> bool:
    email = email.lower()
    return (
        email == "noreply@github.com" or email in TOOL_IDENTITIES or bool(MAILBOX.fullmatch(email))
    )


def has_disallowed(text: str) -> bool:
    return any(not is_allowed(email) for email in EMAIL.findall(text))


def git(*args: str) -> str:
    # Arguments are fixed git operations and validated SHAs; no shell is involved.
    return subprocess.check_output(  # noqa: S603
        ["git", *args],  # noqa: S607
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


def commit_problems(commit: str) -> list[str]:
    problems = []
    identities = git("show", "-s", "--format=%ae%n%ce", commit).splitlines()
    if len(identities) != 2 or any(
        email != "noreply@github.com" and not MAILBOX.fullmatch(email) for email in identities
    ):
        problems.append("non-noreply email")
    trailers = git("show", "-s", "--format=%(trailers:key=Co-authored-by,valueonly)", commit)
    if has_disallowed(trailers):
        problems.append("non-noreply email in trailer")
    message = git("show", "-s", "--format=%B", commit)
    # Skip whole trailer lines already checked so each address is reported once. Matching
    # whole lines, not substrings, keeps an address that merely starts with a trailer value
    # in view.
    values = {line.strip() for line in trailers.splitlines()}
    rest = [
        line
        for line in message.splitlines()
        if not ((m := CO_AUTHOR.fullmatch(line.strip())) and m[1].strip() in values)
    ]
    if has_disallowed("\n".join(rest)):
        problems.append("non-noreply email in message")
    return problems


def check_range(base: str, head: str) -> int:
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        print("Cannot inspect commit range.", file=sys.stderr)
        return 1
    try:
        for revision in (base, head):
            git("rev-parse", "--verify", f"{revision}^{{commit}}")
        commits = dict.fromkeys([*git("rev-list", f"{base}..{head}").splitlines(), head])
        failed = False
        for commit in commits:
            for problem in commit_problems(commit):
                print(f"{commit} {problem}")
                failed = True
        return int(failed)
    except (OSError, subprocess.CalledProcessError):
        print("Cannot inspect commit range.", file=sys.stderr)
        return 1


def check_pr_text() -> int:
    failed = False
    for name, label in (("PR_TITLE", "title"), ("PR_BODY", "body")):
        if has_disallowed(os.environ.get(name, "")):
            print(f"pull request {label} non-noreply email")
            failed = True
    return int(failed)


def main(argv: list[str]) -> int:
    if argv == ["--pr-text"]:
        return check_pr_text()
    if len(argv) == 2:
        return check_range(*argv)
    print("Expected base and head commit SHAs, or --pr-text.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
