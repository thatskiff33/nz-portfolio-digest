"""The CLI and scheduler containers receive the documented application settings."""

import re
from pathlib import Path

import pytest
import yaml

from portfolio_digest.alerts import PushSettings
from portfolio_digest.config import GmailSettings, Settings
from portfolio_digest.mailers import MailSettings

ROOT = Path(__file__).resolve().parents[1]


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
    } & documented
    environment = compose["services"][service]["environment"]
    assert expected <= environment.keys(), sorted(expected - environment.keys())
    for field in GmailSettings.model_fields:
        name = f"GMAIL_{field.upper()}"
        assert environment[name] == "${" + name + ":-}"
