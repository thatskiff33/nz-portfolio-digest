from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_price_timer_runs_twice_daily_in_auckland_time():
    timer = (ROOT / "deploy/systemd/portfolio-digest-prices.timer").read_text()
    assert timer.count("OnCalendar=") == 2
    assert "OnCalendar=*-*-* 10:30:00 Pacific/Auckland" in timer
    assert "OnCalendar=*-*-* 18:30:00 Pacific/Auckland" in timer
    assert "Persistent=true" in timer


def test_scheduled_job_runs_the_same_idempotent_fetch_command_as_the_cli():
    service = (ROOT / "deploy/systemd/portfolio-digest-prices.service").read_text()
    assert "docker compose run --rm migrate prices fetch" in service
