from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_archived_price_timer_runs_twice_daily_in_auckland_time():
    timer = (ROOT / "deploy/legacy/portfolio-digest-prices.timer").read_text()
    assert timer.count("OnCalendar=") == 2
    assert "OnCalendar=*-*-* 10:30:00 Pacific/Auckland" in timer
    assert "OnCalendar=*-*-* 18:30:00 Pacific/Auckland" in timer
    assert "Persistent=true" in timer


def test_archived_price_service_uses_the_idempotent_cli_fetch_command():
    service = (ROOT / "deploy/legacy/portfolio-digest-prices.service").read_text()
    assert "docker compose run --rm migrate prices fetch" in service


def test_compose_passes_market_data_settings_to_the_fetch_container():
    compose = (ROOT / "compose.yaml").read_text()
    assert "PRICE_SOURCE: ${PRICE_SOURCE:-yahoo}" in compose
    assert "FX_SOURCES: ${FX_SOURCES:-yahoo,frankfurter}" in compose


def test_compose_forwards_private_report_settings_and_rotates_all_logs():
    import yaml

    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for name in ("migrate", "scheduler"):
        env = services[name]["environment"]
        for setting in (
            "REPORT_RECIPIENTS",
            "REPORT_KUMA_PUSH_URL",
            "REPORT_PRICES_KUMA_PUSH_URL",
        ):
            assert env[setting] == "${" + setting + ":-}"
    for service in services.values():
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }
