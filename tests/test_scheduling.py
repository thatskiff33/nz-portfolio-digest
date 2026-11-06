"""Calendar boundaries, DST, configuration validation and restart behaviour."""

import json
import re
from datetime import UTC, date, datetime, timedelta

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
        'prices_kuma_push_url="private-token"\nschedules="wrong"',
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


class StopLoop(Exception):
    pass


def seeded_weekly(tmp_path):
    schedule = weekly()
    config = ScheduleConfig(schedules=[schedule])
    path = tmp_path / "state.json"
    due = datetime(2026, 11, 6, 6, tzinfo=UTC)
    path.write_text(
        json.dumps(
            {
                "weekly": due.isoformat(),
                "weekly:config": schedule.model_dump_json() + config.timezone,
            }
        )
    )
    return config, path


def digits_outside_dates(text: str) -> str:
    return re.sub(r"\d{4}-\d{2}-\d{2}", "", text)


def test_failing_job_notifies_once_a_day_and_once_when_it_recovers(tmp_path):
    config, path = seeded_weekly(tmp_path)
    sent = []
    start = datetime(2026, 11, 6, 7, tzinfo=UTC)

    def fail_at(when):
        code = Scheduler(config, path).run(
            lambda *a: (1, ("NZX:MADEUP", "https://monitor.example/secret-token")),
            now=lambda: when,
            sleep=lambda _: pytest.fail("slept after a failure"),
            notify=sent.append,
        )
        assert code == 1

    fail_at(start)
    fail_at(start + timedelta(hours=1))
    fail_at(start + timedelta(hours=23))
    assert len(sent) == 1
    assert "NZX:MADEUP" in sent[0]
    assert "secret-token" not in sent[0]
    assert "https://" not in sent[0]
    stored = json.loads(path.read_text())
    assert stored["weekly"] == datetime(2026, 11, 6, 6, tzinfo=UTC).isoformat()
    assert "weekly:notice" in stored

    fail_at(start + timedelta(hours=24))
    assert len(sent) == 2

    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda *a: 0,
            now=lambda: start + timedelta(hours=25),
            sleep=lambda _: (_ for _ in ()).throw(StopLoop()),
            notify=sent.append,
        )
    assert len(sent) == 3
    assert "recovered" in sent[-1]
    assert "NZX:MADEUP" not in sent[-1]
    assert "weekly:notice" not in json.loads(path.read_text())
    for note in sent:
        assert not re.search(r"\d", digits_outside_dates(note))

    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda *a: 0,
            now=lambda: start + timedelta(hours=26),
            sleep=lambda _: (_ for _ in ()).throw(StopLoop()),
            notify=sent.append,
        )
    assert len(sent) == 3


def test_job_exception_notifies_without_the_error_text(tmp_path):
    config, path = seeded_weekly(tmp_path)
    sent = []

    def explode(*_args):
        raise RuntimeError("provider said 42")

    with pytest.raises(RuntimeError, match="42"):
        Scheduler(config, path).run(
            explode,
            now=lambda: datetime(2026, 11, 6, 7, tzinfo=UTC),
            notify=sent.append,
        )
    assert sent == [
        "The weekly job failed on 2026-11-06.\n\nIt will be retried.",
    ]
    assert not re.search(r"\d", digits_outside_dates(sent[0]))


def test_notice_delivery_failure_does_not_stop_the_scheduler(tmp_path, caplog):
    config, path = seeded_weekly(tmp_path)
    calls = []
    start = datetime(2026, 11, 6, 7, tzinfo=UTC)

    def notify(text):
        calls.append(text)
        raise RuntimeError("mailbox down")

    with caplog.at_level("WARNING"):
        assert (
            Scheduler(config, path).run(
                lambda *a: 1,
                now=lambda: start,
                notify=notify,
            )
            == 1
        )
    assert len(calls) == 1
    assert "could not be delivered" in caplog.text
    assert (
        Scheduler(config, path).run(
            lambda *a: 1,
            now=lambda: start + timedelta(hours=1),
            notify=notify,
        )
        == 1
    )
    assert len(calls) == 1
