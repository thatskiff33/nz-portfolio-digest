"""One TOML schedule format for prices and reports, with Auckland DST handling."""

import json
import logging
import os
import time as clock
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from portfolio_digest.alert_config import AlertConfig
from portfolio_digest.config import ConfigError, get_report_settings, runtime_settings
from portfolio_digest.mailers import address

logger = logging.getLogger(__name__)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Compose marks the scheduler unhealthy when this file is older than 30 minutes.
# It lives on the container's /tmp tmpfs, so a restart does not keep a stale beat.
HEARTBEAT_PATH = Path("/tmp/scheduler-heartbeat")  # noqa: S108 - private container tmpfs


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    kind: Literal["prices", "report"]
    frequency: Literal["daily", "weekly", "monthly", "yearly"]
    at: time
    weekday: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ] = "sunday"
    day: int = Field(default=1, ge=1, le=28)
    month: int = Field(default=4, ge=1, le=12)
    days: int = Field(default=7, ge=1, le=3660)
    period: Literal["days", "last_month", "tax_year"] = "days"

    @field_validator("at")
    @classmethod
    def local_minute(cls, value: time) -> time:
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("use a local HH:MM time")
        return value

    def window(self, today: date) -> tuple[date, date]:
        if self.period == "last_month":
            end = today.replace(day=1) - timedelta(days=1)
            return end.replace(day=1), end
        # Completed calendar days only; never pretend today's markets have all closed.
        return today - timedelta(days=self.days), today - timedelta(days=1)

    def next_run(self, after: datetime, timezone: str = "Pacific/Auckland") -> datetime:
        if after.tzinfo is None:
            raise ValueError("next_run requires an aware datetime")
        zone = ZoneInfo(timezone)
        first = after.astimezone(zone).date()
        for offset in range(370):
            day = first + timedelta(days=offset)
            if self.frequency == "weekly" and day.weekday() != WEEKDAYS.index(self.weekday):
                continue
            if self.frequency == "monthly" and day.day != self.day:
                continue
            if self.frequency == "yearly" and (day.month != self.month or day.day != self.day):
                continue
            local = datetime.combine(day, self.at)
            # Spring gap: run at the first valid local minute after the configured time.
            # Autumn fold: run once, at the first occurrence.
            for minute in range(181):
                candidate = (local + timedelta(minutes=minute)).replace(tzinfo=zone, fold=0)
                utc = candidate.astimezone(UTC)
                if utc.astimezone(zone).replace(tzinfo=None) != candidate.replace(tzinfo=None):
                    continue
                if utc > after.astimezone(UTC):
                    return utc
                break
        raise ConfigError("No next schedule occurrence found.")


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    timezone: str = "Pacific/Auckland"
    recipients: list[str] = Field(default_factory=list)
    kuma_push_url: SecretStr | None = None
    schedules: list[Schedule]
    alerts: AlertConfig = Field(default_factory=AlertConfig)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("recipients")
    @classmethod
    def valid_recipients(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(address(v) for v in values))

    @field_validator("schedules")
    @classmethod
    def unique_names(cls, values: list[Schedule]) -> list[Schedule]:
        if len({s.name for s in values}) != len(values):
            raise ValueError("schedule names must be unique")
        return values

    def named_report(self, name: str) -> Schedule:
        for schedule in self.schedules:
            if schedule.name == name and schedule.kind == "report":
                return schedule
        raise ConfigError("Unknown report schedule. Check the schedule configuration.")

    def report_timing_warnings(self) -> list[str]:
        """Reports that are not strictly after a same-day price fetch.

        Equal times do not count: the fetch and the email would be due together,
        and the email can run first. This checks the configured order only.
        """
        prices = [item for item in self.schedules if item.kind == "prices"]
        warnings = []
        for report in self.schedules:
            if report.kind != "report":
                continue
            earlier = [price for price in prices if price.at < report.at]
            weekly_days = {price.weekday for price in earlier if price.frequency == "weekly"}
            if weekly_days == set(WEEKDAYS) or any(
                _price_runs_on_every_report_day(price, report) for price in earlier
            ):
                continue
            warnings.append(
                f"Report {report.name} at {report.at.strftime('%H:%M')} has no earlier "
                "price fetch on the same day. Move it to after a prices schedule that "
                "runs that day, so the report includes that day's closes."
            )
        return warnings


def _price_runs_on_every_report_day(price: Schedule, report: Schedule) -> bool:
    """True when `price` is due on every local day `report` is due.

    A fetch on a different cadence does not count. The report would sometimes
    be built on a day that fetch does not run.
    """
    if price.frequency == "daily":
        return True
    if price.frequency == "weekly":
        return report.frequency == "weekly" and price.weekday == report.weekday
    if price.frequency == "monthly":
        return report.frequency in {"monthly", "yearly"} and price.day == report.day
    return report.frequency == "yearly" and price.month == report.month and price.day == report.day


def _write_heartbeat(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        logger.warning("Could not write the scheduler heartbeat.")


def read_config(path: Path) -> ScheduleConfig:
    try:
        data = tomllib.loads(path.read_text())
        if "kuma_push_url" in data:
            logger.warning("Move the monitoring setting from TOML to REPORT_KUMA_PUSH_URL in .env.")
        overrides = get_report_settings()
        if overrides.recipients.strip():
            data["recipients"] = [value.strip() for value in overrides.recipients.split(",")]
        if overrides.kuma_push_url and overrides.kuma_push_url.get_secret_value().strip():
            data["kuma_push_url"] = overrides.kuma_push_url
        return ScheduleConfig.model_validate(data)
    except (OSError, ValueError, KeyError, ValidationError):
        raise ConfigError(
            "Cannot read schedule configuration. Check its path and TOML settings."
        ) from None


class SchedulerStateError(ConfigError):
    """A state failure must stop delivery, including inside portfolio handlers."""


@dataclass
class DeliveryCheckpoint:
    """Successful portfolios for one schedule and its original due instant."""

    delivered: set[int]
    save: Callable[[], None]

    def complete(self, portfolio_id: int) -> None:
        self.delivered.add(portfolio_id)
        self.save()


def _identity(config: dict, timezone: str) -> dict:
    keys = ["name", "kind", "frequency", "at"]
    frequency = config["frequency"]
    if frequency == "weekly":
        keys.append("weekday")
    if frequency in {"monthly", "yearly"}:
        keys.append("day")
    if frequency == "yearly":
        keys.append("month")
    return {**{key: config[key] for key in keys}, "timezone": timezone}


def _stored_identity(value) -> dict:
    if isinstance(value, str):
        # Legacy state appended the timezone directly to the serialised model.
        config, end = json.JSONDecoder().raw_decode(value)
        return _identity(config, value[end:])
    if not isinstance(value, dict):
        raise ValueError
    return _identity(value, value["timezone"])


def _timestamp(value) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError
    return result.astimezone(UTC)


@dataclass
class Pending:
    due: datetime
    failures: int = 0
    retry_at: datetime | None = None
    delivered: set[int] = field(default_factory=set)

    @classmethod
    def load(cls, due, retry):
        if not isinstance(retry, dict):
            raise ValueError
        failures = retry.get("failures", 0)
        delivered = retry.get("delivered", [])
        retry_at = retry.get("retry_at")
        if (
            type(failures) is not int
            or failures < 0
            or not isinstance(delivered, list)
            or any(type(value) is not int or value <= 0 for value in delivered)
            or len(set(delivered)) != len(delivered)
            or bool(failures) != (retry_at is not None)
        ):
            raise ValueError
        return cls(
            _timestamp(due),
            failures,
            _timestamp(retry_at) if retry_at is not None else None,
            set(delivered),
        )

    def retry_state(self):
        return {
            "failures": self.failures,
            "retry_at": self.retry_at.isoformat() if self.retry_at else None,
            "delivered": sorted(self.delivered),
        }


@dataclass
class Scheduler:
    config: ScheduleConfig
    state_path: Path

    def run(self, execute, *, now=None, sleep=clock.sleep) -> int:
        if self.config.timezone != runtime_settings().timezone:
            raise ConfigError(
                "TIMEZONE and schedules.toml timezone must agree before starting the scheduler."
            )
        for warning in self.config.report_timing_warnings():
            logger.warning("%s", warning)
        now = now or (lambda: datetime.now(UTC))
        heartbeat = Path(os.environ.get("SCHEDULER_HEARTBEAT", str(HEARTBEAT_PATH)))
        identities = {
            s.name: _identity(s.model_dump(mode="json"), self.config.timezone)
            for s in self.config.schedules
        }
        try:
            state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            if not isinstance(state, dict):
                raise ValueError
            for key in state:
                if ":" in key:
                    name, suffix = key.split(":", 1)
                    if name not in state or suffix not in {"config", "retry"}:
                        raise ValueError
            pending = {}
            for name in state:
                if ":" in name:
                    continue
                saved = Pending.load(state[name], state.get(name + ":retry", {}))
                identity = _stored_identity(state[name + ":config"])
                if name in identities and identity == identities[name]:
                    pending[name] = saved
                else:
                    logger.warning(
                        "Discarded pending scheduler occurrence after configuration change."
                    )
            for schedule in self.config.schedules:
                if schedule.name not in pending:
                    pending[schedule.name] = Pending(schedule.next_run(now(), self.config.timezone))
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            raise SchedulerStateError(
                "Cannot read scheduler state. Check the state volume."
            ) from None

        def save():
            try:
                temporary = self.state_path.with_suffix(".new")
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump(
                        {
                            **{name: item.due.isoformat() for name, item in pending.items()},
                            **{name + ":config": identity for name, identity in identities.items()},
                            **{
                                name + ":retry": item.retry_state()
                                for name, item in pending.items()
                            },
                        },
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.state_path)
            except OSError:
                raise SchedulerStateError(
                    "Cannot write scheduler state. Check the state volume."
                ) from None

        save()
        while True:
            _write_heartbeat(heartbeat)
            for schedule in self.config.schedules:
                item = pending[schedule.name]
                instant = now()
                if item.due > instant or (item.retry_at and item.retry_at > instant):
                    continue
                checkpoint = DeliveryCheckpoint(item.delivered, save)
                try:
                    failed = execute(schedule, item.due, checkpoint) != 0
                except ConfigError:
                    raise
                except Exception:
                    # Job exceptions may contain portfolio figures or provider secrets.
                    failed = True
                finished = now()
                if failed:
                    item.failures += 1
                    delay = min(30 * 2 ** min(item.failures - 1, 4), 360)
                    item.retry_at = finished + timedelta(minutes=delay)
                    logger.warning("Scheduled job failed (%s); retry deferred.", schedule.kind)
                else:
                    next_due = schedule.next_run(item.due, self.config.timezone)
                    if next_due <= finished:
                        logger.warning(
                            "Skipped intervening missed scheduler occurrences (%s).", schedule.kind
                        )
                    pending[schedule.name] = Pending(
                        schedule.next_run(max(instant, finished), self.config.timezone)
                    )
                save()
            sleep(20)
