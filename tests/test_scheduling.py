"""Calendar boundaries, DST, configuration validation and restart behaviour."""

import json
from datetime import UTC, date, datetime

import pytest

from portfolio_digest.config import ConfigError
from portfolio_digest.scheduling import Schedule, ScheduleConfig, Scheduler, read_config


def weekly(at="20:00"):
    return Schedule(name="weekly", kind="report", frequency="weekly", at=at)


def test_next_weekly_run_preserves_local_time_across_spring_dst():
    schedule = weekly()
    assert schedule.next_run(datetime(2026, 9, 20, 9, tzinfo=UTC)) == datetime(
        2026, 9, 27, 7, tzinfo=UTC
    )
    assert schedule.next_run(datetime(2026, 9, 19, 0, tzinfo=UTC)) == datetime(
        2026, 9, 20, 8, tzinfo=UTC
    )


def test_next_weekly_run_preserves_local_time_across_autumn_dst():
    assert weekly().next_run(datetime(2026, 3, 29, 8, tzinfo=UTC)) == datetime(
        2026, 4, 5, 8, tzinfo=UTC
    )


def test_nonexistent_time_moves_to_first_valid_minute_and_fold_runs_once():
    gap = weekly("02:30")
    assert gap.next_run(datetime(2026, 9, 26, 0, tzinfo=UTC)) == datetime(
        2026, 9, 26, 14, tzinfo=UTC
    )  # Sunday 03:00 NZDT
    fold = weekly("02:30")
    first = fold.next_run(datetime(2026, 4, 4, 0, tzinfo=UTC))
    assert first == datetime(2026, 4, 4, 13, 30, tzinfo=UTC)
    assert fold.next_run(first) == datetime(2026, 4, 11, 14, 30, tzinfo=UTC)


def test_monthly_window_includes_leap_day_and_weekly_uses_completed_days():
    monthly = Schedule(
        name="monthly", kind="report", frequency="monthly", at="08:00", period="last_month"
    )
    assert monthly.window(date(2028, 3, 1)) == (date(2028, 2, 1), date(2028, 2, 29))
    assert weekly().window(date(2026, 10, 26)) == (date(2026, 10, 19), date(2026, 10, 25))
    assert monthly.next_run(datetime(2026, 10, 1, tzinfo=UTC)) == datetime(
        2026, 10, 31, 19, tzinfo=UTC
    )


def test_checked_in_config_shares_prices_and_reports():
    from pathlib import Path

    config = read_config(Path(__file__).parent.parent / "schedules.toml")
    assert {s.kind for s in config.schedules} == {"prices", "report"}
    assert config.named_report("weekly").days == 7
    assert config.named_report("monthly").period == "last_month"


@pytest.mark.parametrize(
    "content",
    [
        'timezone="private-token"\nschedules=[]',
        'recipients=["bad\\nprivate-token"]\nschedules=[]',
        'kuma_push_url="private-token"\nschedules="wrong"',
        '[[schedules]]\nname="x"\nkind="report"\nfrequency="daily"\nat="09:00:12"',
    ],
)
def test_config_errors_do_not_echo_inputs(tmp_path, content):
    path = tmp_path / "bad.toml"
    path.write_text(content)
    with pytest.raises(ConfigError) as caught:
        read_config(path)
    assert "private-token" not in str(caught.value)


def test_failed_occurrence_survives_restart_then_advances_after_success(tmp_path):
    schedule = weekly()
    config = ScheduleConfig(schedules=[schedule])
    path = tmp_path / "state.json"
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    path.write_text(
        json.dumps(
            {
                "weekly": due.isoformat(),
                "weekly:config": schedule.model_dump_json() + config.timezone,
            }
        )
    )

    def now():
        return datetime(2026, 10, 25, 8, tzinfo=UTC)

    scheduler = Scheduler(config, path)
    assert scheduler.run(lambda *a: 1, now=now) == 1
    assert json.loads(path.read_text())["weekly"] == due.isoformat()
    calls = []

    class StopLoop(Exception):
        pass

    def sleep(_):
        raise StopLoop

    with pytest.raises(StopLoop):
        scheduler.run(lambda s, at: calls.append(at) or 0, now=now, sleep=sleep)
    assert calls == [due]
    assert datetime.fromisoformat(json.loads(path.read_text())["weekly"]) > now()
    calls.clear()
    with pytest.raises(StopLoop):
        scheduler.run(lambda *a: calls.append(a) or 0, now=now, sleep=sleep)
    assert calls == []


def test_changing_config_replaces_pending_time_on_restart(tmp_path):
    schedule = weekly()
    config = ScheduleConfig(schedules=[schedule])
    path = tmp_path / "state.json"
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    path.write_text(
        json.dumps(
            {
                "weekly": due.isoformat(),
                "weekly:config": weekly("21:00").model_dump_json() + config.timezone,
            }
        )
    )

    class StopLoop(Exception):
        pass

    def sleep(_):
        raise StopLoop

    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda *a: pytest.fail("Obsolete pending job ran"),
            now=lambda: datetime(2026, 10, 25, 8, tzinfo=UTC),
            sleep=sleep,
        )
    assert datetime.fromisoformat(json.loads(path.read_text())["weekly"]) == datetime(
        2026, 11, 1, 7, tzinfo=UTC
    )


def test_annual_year_end_runs_once_at_nine_auckland_across_dst_and_leap_year():
    from pathlib import Path

    schedule = read_config(Path("schedules.toml")).named_report("year-end")
    assert schedule.period == "tax_year"
    # 1 April is still NZDT in 2026 and 2027, but NZST in 2029.
    first = datetime(2026, 3, 31, 20, tzinfo=UTC)
    assert schedule.next_run(datetime(2026, 1, 1, tzinfo=UTC)) == first
    assert schedule.next_run(first) == datetime(2027, 3, 31, 20, tzinfo=UTC)
    assert schedule.next_run(datetime(2028, 3, 31, 20, tzinfo=UTC)) == datetime(
        2029, 3, 31, 21, tzinfo=UTC
    )
