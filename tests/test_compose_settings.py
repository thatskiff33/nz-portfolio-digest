"""The CLI and scheduler containers receive the documented application settings."""

import re
from pathlib import Path

import pytest
import yaml

from portfolio_digest.alerts import PushSettings
from portfolio_digest.config import GmailSettings, Settings
from portfolio_digest.mailers import MailSettings

ROOT = Path(__file__).resolve().parents[1]


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys that PyYAML normally silently overwrites."""

    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in keys:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


@pytest.mark.parametrize(
    "document",
    [
        "services: {}\nservices: {}\n",
        "services:\n  migrate:\n    environment:\n      GMAIL_CLIENT_ID: first\n"
        "      GMAIL_CLIENT_ID: second\n",
    ],
)
def test_compose_loader_rejects_duplicate_mapping_keys(document):
    with pytest.raises(yaml.constructor.ConstructorError, match="found duplicate key"):
        yaml.load(document, Loader=UniqueKeySafeLoader)  # noqa: S506 - SafeLoader subclass


@pytest.mark.parametrize("service", ["migrate", "scheduler"])
def test_compose_passes_documented_app_settings(service):
    compose = yaml.load(
        (ROOT / "compose.yaml").read_text(),
        Loader=UniqueKeySafeLoader,  # noqa: S506 - SafeLoader subclass
    )
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
