"""Containers receive the documented settings for their services."""

import re
from pathlib import Path

import pytest
import yaml

from portfolio_digest.alerts import PushSettings
from portfolio_digest.config import GmailSettings, Settings
from portfolio_digest.mailers import MailSettings

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_SETTINGS = {
    "DASHBOARD_PASSWORD": "",
    "DASHBOARD_BIND": "127.0.0.1",
    "DASHBOARD_SECURE_COOKIE": "false",
    "DASHBOARD_HOSTNAMES": "",
}


@pytest.mark.parametrize("service", ["migrate", "scheduler"])
def test_compose_passes_documented_app_settings(service):
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    documented = set(
        re.findall(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.M)
    )
    expected = {
        (model.model_config.get("env_prefix", "") + name).upper()
        for model in (Settings, GmailSettings, MailSettings, PushSettings)
        for name in model.model_fields
    } & documented - DASHBOARD_SETTINGS.keys()
    environment = compose["services"][service]["environment"]
    assert expected <= environment.keys(), sorted(expected - environment.keys())
    for field in GmailSettings.model_fields:
        name = f"GMAIL_{field.upper()}"
        assert environment[name] == "${" + name + ":-}"


def test_compose_passes_dashboard_settings_only_to_web():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    dashboard_fields = {
        name.upper() for name in Settings.model_fields if name.startswith("dashboard_")
    }
    assert dashboard_fields == DASHBOARD_SETTINGS.keys()
    for name, default in DASHBOARD_SETTINGS.items():
        assert compose["services"]["web"]["environment"][name] == "${" + name + ":-" + default + "}"
    for service, config in compose["services"].items():
        if service != "web":
            assert dashboard_fields.isdisjoint(config.get("environment", {})), service
