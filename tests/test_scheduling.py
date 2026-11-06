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
    assert {s.name for s in config.schedules if s.kind == "prices"} == {
        "prices-morning",
        "prices-evening",
    }
    assert config.named_report("year-end").period == "tax_year"
    assert config.alerts.enabled


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


def test_annual_year_end_runs_once_at_nineteen_auckland_across_dst_and_leap_year():
    from pathlib import Path

    schedule = read_config(Path("schedules.toml")).named_report("year-end")
    assert schedule.period == "tax_year"
    # 1 April is still NZDT in 2026 and 2027, but NZST in 2029.
    first = datetime(2026, 4, 1, 6, tzinfo=UTC)
    assert schedule.next_run(datetime(2026, 1, 1, tzinfo=UTC)) == first
    assert schedule.next_run(first) == datetime(2027, 4, 1, 6, tzinfo=UTC)
    assert schedule.next_run(datetime(2028, 4, 1, 6, tzinfo=UTC)) == datetime(
        2029, 4, 1, 7, tzinfo=UTC
    )


def test_shared_scheduler_runs_every_configured_job_after_downtime(tmp_path):
    from pathlib import Path

    config = read_config(Path("schedules.toml"))
    due = datetime(2026, 3, 31, 20, tzinfo=UTC)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                **{s.name: due.isoformat() for s in config.schedules},
                **{
                    s.name + ":config": s.model_dump_json() + config.timezone
                    for s in config.schedules
                },
            }
        )
    )
    calls = []

    class StopLoop(Exception):
        pass

    def sleep(_):
        raise StopLoop

    with pytest.raises(StopLoop):
        Scheduler(config, state_path).run(
            lambda schedule, at: calls.append((schedule.name, at)) or 0,
            now=lambda: due,
            sleep=sleep,
        )
    assert set(calls) == {
        ("prices-morning", due),
        ("prices-evening", due),
        ("weekly", due),
        ("monthly", due),
        ("year-end", due),
    }
    state = json.loads(state_path.read_text())
    assert all(datetime.fromisoformat(state[s.name]) > due for s in config.schedules)


@pytest.mark.parametrize(
    "report_time,price_time,price_frequency,price_options,warns",
    [
        ("08:00", "10:30", "daily", {}, True),
        ("10:30", "10:30", "daily", {}, True),
        ("19:00", "10:30", "daily", {}, False),
        ("19:00", "10:30", "weekly", {"weekday": "monday"}, True),
        ("19:00", "10:30", "monthly", {"day": 2}, True),
        ("19:00", "10:30", "monthly", {"day": 1}, False),
        ("19:00", "10:30", "yearly", {"day": 1, "month": 4}, True),
    ],
)
def test_report_timing_warning_at_startup(
    tmp_path, caplog, report_time, price_time, price_frequency, price_options, warns
):
    config = ScheduleConfig(
        schedules=[
            Schedule(name="monthly", kind="report", frequency="monthly", at=report_time),
            Schedule(
                name="prices",
                kind="prices",
                frequency=price_frequency,
                at=price_time,
                **price_options,
            ),
        ]
    )

    class StopLoop(Exception):
        pass

    def stop(_):
        raise StopLoop

    with pytest.raises(StopLoop):
        Scheduler(config, tmp_path / "state.json").run(
            lambda *args: 0, now=lambda: datetime(2026, 11, 1, tzinfo=UTC), sleep=stop
        )
    assert ("Report monthly at" in caplog.text) == warns
    if warns:
        assert caplog.text.count("Report monthly at") == 1
        assert report_time in caplog.text


def test_default_report_times_and_startup_have_no_timing_warnings(tmp_path, caplog):
    from datetime import time
    from pathlib import Path

    config = read_config(Path("schedules.toml"))
    assert config.named_report("monthly").at == time(19)
    assert config.named_report("year-end").at == time(19)
    assert config.report_timing_warnings() == []

    class StopLoop(Exception):
        pass

    def stop(_):
        raise StopLoop

    with pytest.raises(StopLoop):
        Scheduler(config, tmp_path / "state.json").run(
            lambda *args: 0, now=lambda: datetime(2026, 11, 1, tzinfo=UTC), sleep=stop
        )
    assert not caplog.records


def test_collective_weekly_fetches_cover_daily_reports():
    from portfolio_digest.scheduling import WEEKDAYS

    config = ScheduleConfig(
        schedules=[
            Schedule(name="daily", kind="report", frequency="daily", at="19:00"),
            *(
                Schedule(name=day, kind="prices", frequency="weekly", weekday=day, at="10:30")
                for day in WEEKDAYS
            ),
        ]
    )
    assert config.report_timing_warnings() == []
    config.schedules.pop()
    assert config.report_timing_warnings()
