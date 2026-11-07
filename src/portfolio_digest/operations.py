"""Short operations notices for failed jobs. No balances, prices or links."""

import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

from portfolio_digest.alerts import push
from portfolio_digest.config import ConfigError
from portfolio_digest.mailers import mail_settings, message, send

logger = logging.getLogger(__name__)

THROTTLE = timedelta(hours=24)
# A listing such as NZX:MADEUP, or one currency pair such as USD/NZD.
_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,63}(?:/[A-Za-z][A-Za-z0-9._:-]{0,63})?$")


def _spoken_identifier(value: str) -> str:
    # Spell digits in identifiers so notices cannot be mistaken for financial figures.
    words = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
    return value.translate(str.maketrans(dict(zip("0123456789", words, strict=True))))


def safe_labels(labels: Sequence[str]) -> tuple[str, ...]:
    """Keep instrument and currency labels. Drop anything that could be a link or a figure."""
    kept: list[str] = []
    for label in labels:
        head = label.split(" (", 1)[0].strip()
        if "://" in head or not _LABEL.fullmatch(head):
            continue
        kept.append(_spoken_identifier(head))
    return tuple(dict.fromkeys(kept))


def _day(when: datetime, timezone: str) -> str:
    from zoneinfo import ZoneInfo

    return when.astimezone(ZoneInfo(timezone)).date().isoformat()


def failure_text(job: str, when: datetime, timezone: str, labels: Sequence[str] = ()) -> str:
    lines = [f"The {_spoken_identifier(job)} job failed on {_day(when, timezone)}."]
    safe = safe_labels(labels)
    if safe:
        lines.extend(["", "These failed: " + ", ".join(safe) + "."])
    lines.extend(["", "It will be retried."])
    return "\n".join(lines)


def recovery_text(job: str, when: datetime, timezone: str) -> str:
    return (
        f"The {_spoken_identifier(job)} job recovered on {_day(when, timezone)}."
        "\n\nThe latest run succeeded."
    )


def deliver_operations_notice(config, text: str, *, recovered: bool) -> None:
    """Email the notice when the mailer is configured, then try the optional push.

    A delivery problem is logged and never raised. The text is sent as given, so callers
    must already have removed figures and links.
    """
    subject = "Portfolio job recovered" if recovered else "Portfolio job failed"
    try:
        settings = mail_settings()
        recipients = list(config.recipients)
        if not recipients and settings.mail_owner:
            recipients = [settings.mail_owner]
        if settings.mail_from and recipients:
            html = (
                '<html lang="en-NZ"><body style="font-family:Arial,sans-serif">'
                + "".join(f"<p>{escape(line)}</p>" for line in text.splitlines() if line)
                + "</body></html>"
            )
            send(settings, message(settings.mail_from, recipients, subject, html, text), recipients)
        else:
            logger.info("Operations notice was not emailed because mail is not configured.")
    except Exception:
        logger.warning("Operations notice could not be emailed.")
    push(text)


def load_notice_config():
    """Recipients from the schedule file, or an empty list so MAIL_OWNER can be used."""
    from portfolio_digest.scheduling import ScheduleConfig, read_config

    path = Path(os.environ.get("SCHEDULE_CONFIG", "schedules.toml"))
    try:
        if path.is_file():
            return read_config(path)
    except ConfigError:
        logger.warning("Operations notice could not read schedule recipients.")
    return ScheduleConfig(schedules=[])


def _notices_path() -> Path:
    directory = Path(os.environ.get("SCHEDULER_STATE", "state/scheduler.json")).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / "operations-notices.json"


def _load_notices(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("Operations notice state could not be read.")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, str)
    }


def _save_notices(path: Path, data: dict[str, str]) -> None:
    try:
        temporary = path.with_suffix(".new")
        temporary.write_text(json.dumps(data))
        temporary.replace(path)
    except OSError:
        logger.warning("Operations notice state could not be saved.")


def _aware(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def note_outcome(
    config,
    job: str,
    *,
    failed: bool,
    labels: Sequence[str] = (),
    now: datetime | None = None,
) -> None:
    """Send at most one failure notice per job per day, then one recovery notice.

    Used by commands that do not run inside the scheduler. The timestamp lives next to
    the scheduler state. A lock serialises overlapping manual and Akahu commands so
    they cannot send duplicate notices or overwrite each other's throttle state.
    Windows skips the Unix-only lock.
    """
    try:
        path = _notices_path()
        if sys.platform == "win32":
            _note_outcome(config, job, failed=failed, labels=labels, now=now, path=path)
            return
        import fcntl

        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _note_outcome(config, job, failed=failed, labels=labels, now=now, path=path)
    except Exception:
        logger.warning("Operations notice could not be delivered.")


def _note_outcome(
    config, job: str, *, failed: bool, labels: Sequence[str], now: datetime | None, path: Path
) -> None:
    now = now or datetime.now(UTC)
    timezone = getattr(config, "timezone", None) or "Pacific/Auckland"
    data = _load_notices(path)
    last = _aware(data[job]) if job in data else None
    if failed:
        if last is not None and now < last + THROTTLE:
            return
        deliver_operations_notice(config, failure_text(job, now, timezone, labels), recovered=False)
        data[job] = now.isoformat()
        _save_notices(path, data)
        return
    if job not in data:
        return
    deliver_operations_notice(config, recovery_text(job, now, timezone), recovered=True)
    data.pop(job, None)
    _save_notices(path, data)
