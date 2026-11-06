"""Calendar boundaries, DST, configuration validation and restart behaviour."""

import json
import os
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


class StopLoop(Exception):
    pass


def stop(_):
    raise StopLoop


def seed(path, config, due):
    path.write_text(
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


@pytest.mark.parametrize("throws", [False, True])
def test_failure_retries_independently_and_survives_restart(tmp_path, throws):
    prices = Schedule(name="prices", kind="prices", frequency="daily", at="09:00")
    config = ScheduleConfig(schedules=[prices, weekly()])
    path = tmp_path / "state.json"
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    seed(path, config, due)
    instant = due
    calls = []

    def execute(schedule, at, checkpoint):
        calls.append((schedule.name, at))
        if schedule.kind == "prices":
            if throws:
                raise RuntimeError("invented-private-token")
            return 1
        return 0

    def run(callback=execute):
        with pytest.raises(StopLoop):
            Scheduler(config, path).run(callback, now=lambda: instant, sleep=stop)

    run()
    assert calls == [("prices", due), ("weekly", due)]
    run()
    assert len(calls) == 2
    for minutes in [30, 60, 120, 240, 360, 360]:
        instant += timedelta(minutes=minutes)
        run()
        assert calls[-1] == ("prices", due)
        previous_count = len(calls)
        run()
        assert len(calls) == previous_count
    instant += timedelta(hours=6)
    run(lambda *args: 0)
    state = json.loads(path.read_text())
    assert datetime.fromisoformat(state["prices"]) > instant
    assert state["prices:retry"]["failures"] == 0
    assert state["prices:retry"]["delivered"] == []


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
            lambda schedule, at, checkpoint: calls.append((schedule.name, at)) or 0,
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


def test_legacy_extra_config_field_preserves_pending_and_logs_skips(tmp_path, caplog):
    schedule = weekly()
    config = ScheduleConfig(schedules=[schedule])
    path = tmp_path / "state.json"
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    legacy = {**schedule.model_dump(mode="json"), "future_option": "invented"}
    path.write_text(
        json.dumps(
            {
                "weekly": due.isoformat(),
                "weekly:config": json.dumps(legacy) + config.timezone,
            }
        )
    )
    calls = []
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda s, at, checkpoint: calls.append(at) or 0,
            now=lambda: due + timedelta(days=15),
            sleep=stop,
        )
    assert calls == [due]
    assert "Skipped intervening" in caplog.text
    assert "Discarded" not in caplog.text


@pytest.mark.parametrize("removed", [False, True])
def test_changed_or_removed_schedule_logs_discard(tmp_path, caplog, removed):
    path = tmp_path / "state.json"
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    seed(path, ScheduleConfig(schedules=[weekly("21:00")]), due)
    config = ScheduleConfig(schedules=[] if removed else [weekly()])
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(lambda *a: 0, now=lambda: due, sleep=stop)
    assert "Discarded pending" in caplog.text


@pytest.mark.parametrize(
    "retry",
    [
        {"failures": -1},
        {"failures": True},
        {"failures": 1},
        {"retry_at": "invented-private-token"},
        {"failures": 1, "retry_at": "2026-10-25T07:00:00"},
        {"delivered": [True]},
        {"delivered": ["invented-private-token"]},
        {"delivered": [0]},
        {"delivered": [1, 1]},
        {"delivered": {}},
    ],
)
def test_invalid_retry_state_is_fatal_and_sanitised(tmp_path, retry):
    path = tmp_path / "state.json"
    config = ScheduleConfig(schedules=[weekly()])
    seed(path, config, datetime(2026, 10, 25, 7, tzinfo=UTC))
    state = json.loads(path.read_text())
    state["weekly:retry"] = retry
    path.write_text(json.dumps(state))
    with pytest.raises(ConfigError, match="Cannot read scheduler state") as error:
        Scheduler(config, path).run(lambda *a: pytest.fail("Executed malformed state"))
    assert "invented-private-token" not in str(error.value)


def test_checkpoint_is_durable_and_fsync_precedes_replace(tmp_path, monkeypatch):
    from pathlib import Path

    from portfolio_digest import scheduling

    path = tmp_path / "state.json"
    config = ScheduleConfig(schedules=[weekly()])
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    seed(path, config, due)
    events = []
    fsync = scheduling.os.fsync
    replace = Path.replace

    def sync(fd):
        events.append("sync")
        fsync(fd)

    def move(source, target):
        assert events[-1] == "sync"
        events.append("replace")
        return replace(source, target)

    monkeypatch.setattr(scheduling.os, "fsync", sync)
    monkeypatch.setattr(Path, "replace", move)

    def execute(s, at, checkpoint):
        checkpoint.complete(7)
        assert json.loads(path.read_text())["weekly:retry"]["delivered"] == [7]
        return 1

    with pytest.raises(StopLoop):
        Scheduler(config, path).run(execute, now=lambda: due, sleep=stop)
    delivered = []
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda s, at, cp: delivered.extend(cp.delivered) or 0,
            now=lambda: due + timedelta(minutes=30),
            sleep=stop,
        )
    assert delivered == [7]
    assert json.loads(path.read_text())["weekly:retry"]["delivered"] == []


@pytest.mark.parametrize("stage", ["open", "fsync", "replace"])
def test_checkpoint_write_error_is_fatal_and_preserves_previous_file(tmp_path, monkeypatch, stage):
    from pathlib import Path

    from portfolio_digest import scheduling
    from portfolio_digest.scheduling import SchedulerStateError

    path = tmp_path / "state.json"
    config = ScheduleConfig(schedules=[weekly()])
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    seed(path, config, due)

    def execute(s, at, checkpoint):
        def fail(*args, **kwargs):
            raise OSError("invented-private-token")

        if stage == "fsync":
            monkeypatch.setattr(scheduling.os, "fsync", fail)
        elif stage == "replace":
            monkeypatch.setattr(Path, "replace", fail)
        else:
            original_open = Path.open

            def open_file(path, *args, **kwargs):
                if path.suffix == ".new":
                    fail()
                return original_open(path, *args, **kwargs)

            monkeypatch.setattr(Path, "open", open_file)
        checkpoint.complete(1)

    with pytest.raises(SchedulerStateError, match="Cannot write scheduler state"):
        Scheduler(config, path).run(execute, now=lambda: due, sleep=stop)
    assert json.loads(path.read_text())["weekly:retry"]["delivered"] == []


def test_backoff_starts_when_attempt_finishes_and_logs_are_private(tmp_path, caplog):
    path = tmp_path / "state.json"
    config = ScheduleConfig(schedules=[weekly()])
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    seed(path, config, due)
    instant = due
    calls = []

    def execute(*args):
        nonlocal instant
        calls.append(instant)
        instant += timedelta(minutes=15)
        raise RuntimeError("invented-private-token")

    with pytest.raises(StopLoop):
        Scheduler(config, path).run(execute, now=lambda: instant, sleep=stop)
    instant = due + timedelta(minutes=30)
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(execute, now=lambda: instant, sleep=stop)
    assert len(calls) == 1
    instant = due + timedelta(minutes=45)
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(execute, now=lambda: instant, sleep=stop)
    assert len(calls) == 2
    assert "retry deferred" in caplog.text
    assert "invented-private-token" not in caplog.text


def test_configuration_error_stops_scheduler_without_retry(tmp_path):
    config = ScheduleConfig(schedules=[weekly()])
    path = tmp_path / "state.json"
    due = datetime(2026, 10, 25, 7, tzinfo=UTC)
    seed(path, config, due)

    def execute(*args):
        raise ConfigError("Invalid configuration.")

    with pytest.raises(ConfigError, match="Invalid configuration"):
        Scheduler(config, path).run(execute, now=lambda: due, sleep=stop)
    assert json.loads(path.read_text())["weekly:retry"]["failures"] == 0


def test_scheduler_writes_its_heartbeat_each_cycle(tmp_path, monkeypatch):
    config = ScheduleConfig(schedules=[weekly()])
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setenv("SCHEDULER_HEARTBEAT", str(heartbeat))
    cycles = 0

    def sleep(_):
        nonlocal cycles
        cycles += 1
        assert heartbeat.is_file()
        if cycles == 1:
            aged = heartbeat.stat().st_mtime - 60
            os.utime(heartbeat, (aged, aged))
            return
        raise StopLoop

    with pytest.raises(StopLoop):
        Scheduler(config, tmp_path / "state.json").run(
            lambda *_args: pytest.fail("No job is due this cycle"),
            now=lambda: datetime(2026, 10, 25, 8, tzinfo=UTC),
            sleep=sleep,
        )
    assert cycles == 2
    assert heartbeat.stat().st_mtime > datetime.now().timestamp() - 30


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
        with pytest.raises(StopLoop):
            Scheduler(config, path).run(
                lambda *a: (1, ("NZX:MADEUP", "https://monitor.example/secret-token")),
                now=lambda: when,
                sleep=stop,
                notify=sent.append,
            )

    fail_at(start)
    fail_at(start + timedelta(hours=1))
    fail_at(start + timedelta(hours=22))
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
            now=lambda: start + timedelta(hours=30),
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
            now=lambda: start + timedelta(hours=31),
            sleep=lambda _: (_ for _ in ()).throw(StopLoop()),
            notify=sent.append,
        )
    assert len(sent) == 3


def test_job_exception_notifies_without_the_error_text(tmp_path):
    config, path = seeded_weekly(tmp_path)
    sent = []

    def explode(*_args):
        raise RuntimeError("provider said 42")

    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            explode,
            now=lambda: datetime(2026, 11, 6, 7, tzinfo=UTC),
            notify=sent.append,
            sleep=stop,
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

    with caplog.at_level("WARNING"), pytest.raises(StopLoop):
        Scheduler(config, path).run(lambda *a: 1, now=lambda: start, notify=notify, sleep=stop)
    assert len(calls) == 1
    assert "could not be delivered" in caplog.text
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda *a: 1, now=lambda: start + timedelta(hours=1), notify=notify, sleep=stop
        )
    assert len(calls) == 1


def test_partial_price_fetch_advances_but_recovers_only_after_clean_fetch(tmp_path):
    schedule = Schedule(name="prices", kind="prices", frequency="daily", at="20:00")
    config = ScheduleConfig(schedules=[schedule])
    path = tmp_path / "state.json"
    due = datetime(2026, 11, 6, 7, tzinfo=UTC)
    seed(path, config, due)
    sent = []
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda *a: (0, ("NZX:MADEUP",)), now=lambda: due, sleep=stop, notify=sent.append
        )
    state = json.loads(path.read_text())
    assert state["prices:retry"]["failures"] == 0
    assert datetime.fromisoformat(state["prices"]) > due
    assert len(sent) == 1
    assert "NZX:MADEUP" in sent[0]

    # Restart before the next occurrence neither retries nor announces recovery.
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda *a: pytest.fail("Partial fetch was retried"),
            now=lambda: due + timedelta(hours=1),
            sleep=stop,
            notify=sent.append,
        )
    assert len(sent) == 1
    with pytest.raises(StopLoop):
        Scheduler(config, path).run(
            lambda *a: (0, ()),
            now=lambda: due + timedelta(days=1),
            sleep=stop,
            notify=sent.append,
        )
    assert len(sent) == 2
    assert "recovered" in sent[-1]
    assert "prices:notice" not in json.loads(path.read_text())
