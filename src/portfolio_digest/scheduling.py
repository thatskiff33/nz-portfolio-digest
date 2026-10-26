"""One TOML schedule format for prices and reports, with Auckland DST handling."""

import json
import logging
import time as clock
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from portfolio_digest.config import ConfigError
from portfolio_digest.mailers import address

logger = logging.getLogger(__name__)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    kind: Literal["prices", "report"]
    frequency: Literal["daily", "weekly", "monthly"]
    at: time
    weekday: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ] = "sunday"
    day: int = Field(default=1, ge=1, le=28)
    days: int = Field(default=7, ge=1, le=3660)
    period: Literal["days", "last_month"] = "days"

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


def read_config(path: Path) -> ScheduleConfig:
    try:
        return ScheduleConfig.model_validate(tomllib.loads(path.read_text()))
    except (OSError, ValueError, KeyError, ValidationError):
        raise ConfigError(
            "Cannot read schedule configuration. Check its path and TOML settings."
        ) from None


@dataclass
class Scheduler:
    config: ScheduleConfig
    state_path: Path

    def run(self, execute, *, now=None, sleep=clock.sleep) -> int:
        now = now or (lambda: datetime.now(UTC))
        try:
            state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            if not isinstance(state, dict):
                raise ValueError
            due = {
                s.name: datetime.fromisoformat(state[s.name])
                if (
                    s.name in state
                    and state.get(s.name + ":config") == s.model_dump_json() + self.config.timezone
                )
                else s.next_run(now(), self.config.timezone)
                for s in self.config.schedules
            }
            if any(value.tzinfo is None for value in due.values()):
                raise ValueError
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError, TypeError):
            raise ConfigError("Cannot read scheduler state. Check the state volume.") from None

        def save():
            try:
                temporary = self.state_path.with_suffix(".new")
                temporary.write_text(
                    json.dumps(
                        {
                            **{name: day.isoformat() for name, day in due.items()},
                            **{
                                s.name + ":config": s.model_dump_json() + self.config.timezone
                                for s in self.config.schedules
                            },
                        }
                    )
                )
                temporary.replace(self.state_path)
            except OSError:
                raise ConfigError("Cannot write scheduler state. Check the state volume.") from None

        # Persist pending occurrences before delivery, so restarts retry failed jobs.
        save()
        while True:
            instant = now()
            for schedule in self.config.schedules:
                scheduled = due[schedule.name]
                if scheduled > instant:
                    continue
                # One catch-up per schedule after downtime, retaining its original period.
                if execute(schedule, scheduled) != 0:
                    logger.error("Scheduled job failed (%s).", schedule.kind)
                    return 1
                due[schedule.name] = schedule.next_run(max(instant, now()), self.config.timezone)
                save()
            sleep(20)
