"""Reject non-noreply PR commit identities without printing private metadata."""

import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-fA-F]{40}")
MAILBOX = re.compile(r"[^\s@<>]+@users\.noreply\.github\.com")


def git(*args: str) -> str:
    # Arguments are fixed git operations and validated SHAs; no shell is involved.
    return subprocess.check_output(  # noqa: S603
        ["git", *args],  # noqa: S607
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


def main(base: str, head: str) -> int:
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        print("Cannot inspect commit range.", file=sys.stderr)
        return 1
    try:
        for revision in (base, head):
            git("rev-parse", "--verify", f"{revision}^{{commit}}")
        commits = dict.fromkeys([*git("rev-list", f"{base}..{head}").splitlines(), head])
        failed = False
        for commit in commits:
            emails = git("show", "-s", "--format=%ae%n%ce", commit).splitlines()
            if len(emails) != 2 or any(
                email != "noreply@github.com" and not MAILBOX.fullmatch(email) for email in emails
            ):
                print(f"{commit} non-noreply email")
                failed = True
        return int(failed)
    except (OSError, subprocess.CalledProcessError):
        print("Cannot inspect commit range.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Expected base and head commit SHAs.")
    sys.exit(main(*sys.argv[1:]))
