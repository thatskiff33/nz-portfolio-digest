"""Exercise the remote deployment body with stand-in Git and Compose commands."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX shell")


@pytest.fixture
def deployment(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "git": """
printf 'git %s\n' "$*" >> "$LOG"
case "$*" in
  'branch --show-current') echo main ;;
  'rev-parse HEAD') echo invented-release ;;
  'restore schedules.toml') rm schedules.toml; echo public > schedules.toml ;;
  'pull --ff-only origin main') if [ "$FAIL" = git-pull ]; then exit 1; fi ;;
esac
""",
        "docker": """
printf '%s\n' "$*" >> "$LOG"
case "$*" in
  'compose ps --all --quiet web') printf '%s' "$WEB_CONTAINER" ;;
  'compose exec -T db psql '*) echo invented-revision ;;
  'compose run --rm --entrypoint sh backup -c '*) cat > "$METADATA" ;;
esac
if [ -n "$FAIL" ]; then
  case "$*" in *"$FAIL"*) exit 1 ;; esac
fi
""",
        "systemctl": "exit 0\n",
    }.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
    (tmp_path / "schedules.toml").write_text("private schedule")
    return tmp_path, bin_dir


def deploy(deployment, password=None, container="", fail=""):
    root, bin_dir = deployment
    if password is not None:
        (root / ".env").write_text(password)
    # Run the actual heredoc body, bypassing only the SSH transport.
    body = SCRIPT.read_text().split("<<'REMOTE'\n", 1)[1].rsplit("\nREMOTE", 1)[0]
    result = subprocess.run(  # noqa: S603
        [SH, "-s", "--", str(root)],
        input="umask 077\n" + body,
        text=True,
        capture_output=True,
        cwd=root,
        timeout=20,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "LOG": str(root / "log"),
            "METADATA": str(root / "metadata"),
            "WEB_CONTAINER": container,
            "FAIL": fail,
        },
    )
    assert "invented-secret" not in result.stdout + result.stderr
    assert (root / "schedules.toml").read_text() == "private schedule"
    if os.name != "nt":
        assert (root / "schedules.toml").stat().st_mode & 0o777 == 0o644
    return result, (root / "log").read_text().splitlines()


@pytest.mark.parametrize("container", ["running-web", "stopped-web"])
def test_existing_dashboard_is_rebuilt_and_restarted(deployment, container):
    result, log = deploy(deployment, container=container)
    assert result.returncode == 0, result.stderr
    commands = [
        "compose run --rm backup --once",
        "git pull --ff-only origin main",
        "compose build migrate scheduler web",
        "compose stop scheduler web",
        "compose run --rm migrate",
        "compose run --rm migrate health",
        "compose up -d --no-deps db backup scheduler web",
    ]
    positions = [log.index(command) for command in commands]
    assert positions == sorted(positions)
    metadata = (deployment[0] / "metadata").read_text()
    assert "invented-release" in metadata and "invented-revision" in metadata


@pytest.mark.parametrize(
    "password,enabled",
    [
        (None, False),
        ("", False),
        ("DASHBOARD_PASSWORD=\n", False),
        ('DASHBOARD_PASSWORD=""\n', False),
        ("DASHBOARD_PASSWORD=''\n", False),
        ("DASHBOARD_PASSWORD= # absent\n", False),
        ('DASHBOARD_PASSWORD="" # absent\n', False),
        ("DASHBOARD_PASSWORD=invented-secret\n", True),
        (' export DASHBOARD_PASSWORD = "invented-secret" # private\n', True),
        ("DASHBOARD_PASSWORD='invented-secret'\n", True),
        ("DASHBOARD_PASSWORD=invented-secret\nDASHBOARD_PASSWORD=\n", False),
        ("DASHBOARD_PASSWORD=$(touch executed)\n", True),
    ],
)
def test_password_detection_is_private_and_does_not_execute_env(deployment, password, enabled):
    result, log = deploy(deployment, password=password)
    assert result.returncode == 0, result.stderr
    suffix = " web" if enabled else ""
    assert f"compose build migrate scheduler{suffix}" in log
    assert f"compose up -d --no-deps db backup scheduler{suffix}" in log
    assert not (deployment[0] / "executed").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "git-pull",
        "backup --once",
        "compose build",
        "compose run --rm migrate",
        "compose run --rm migrate health",
    ],
)
def test_failed_deploy_never_restarts_services(deployment, failure):
    result, log = deploy(deployment, container="stopped-web", fail=failure)
    assert result.returncode != 0
    assert not any(command.startswith("compose up") for command in log)
    if failure in {"compose run --rm migrate", "compose run --rm migrate health"}:
        assert "compose stop scheduler web" in log


def test_deploy_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is installed by the verification environment")
    remote = SCRIPT.read_text().split("<<'REMOTE'\n", 1)[1].rsplit("\nREMOTE", 1)[0]
    for command, body in [
        ([shellcheck, str(SCRIPT)], None),
        ([shellcheck, "--shell=sh", "-"], remote),
    ]:
        result = subprocess.run(  # noqa: S603
            command, input=body, text=True, capture_output=True, timeout=20
        )
        assert result.returncode == 0, result.stdout + result.stderr
