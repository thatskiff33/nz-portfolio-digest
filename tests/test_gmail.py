import base64
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from pydantic import SecretStr

from portfolio_digest.config import GmailSettings
from portfolio_digest.gmail import SCOPE, AsbGmailImporter, GmailClient, GmailError

FIXTURES = Path(__file__).parent / "fixtures"


class FakeGoogle:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if "oauth2.googleapis.com" in url:
            fields = parse_qs(body.decode())
            assert fields["client_secret"] == ["made-up-secret"]
            return {"access_token": "made-up-access", "scope": SCOPE}
        assert headers == {"Authorization": "Bearer made-up-access"}
        if url.endswith("/labels"):
            return {"labels": [{"id": "Label_42", "name": "ASB notes"}]}
        if "/messages?" in url:
            return {"messages": [{"id": "made-up-message"}]}
        raw = base64.urlsafe_b64encode((FIXTURES / "asb-contract-note.eml").read_bytes()).decode()
        return {"raw": raw.rstrip("=")}


def settings() -> GmailSettings:
    return GmailSettings(
        client_id="made-up-client",
        client_secret=SecretStr("made-up-secret"),
        refresh_token=SecretStr("made-up-refresh"),
        label="ASB notes",
    )


def test_gmail_feed_reads_only_the_configured_label_and_parses_contract_notes():
    google = FakeGoogle()
    result = AsbGmailImporter(GmailClient(settings(), google), "ASB notes").fetch()

    assert [trade.external_ref for trade in result.trades] == ["contract:40012345"]
    assert result.errors == []
    assert any("labelIds=Label_42" in call[1] for call in google.calls)


def test_gmail_refuses_a_token_with_any_scope_beyond_read_only():
    def over_scoped(method, url, headers, body):
        return {"access_token": "do-not-use", "scope": SCOPE + " https://mail.google.com/"}

    client = GmailClient(settings(), over_scoped)
    with pytest.raises(GmailError, match="more than read-only"):
        client.label_id("ASB notes")


def test_gmail_errors_do_not_include_oauth_credentials():
    def broken(method, url, headers, body):
        raise GmailError("Google refused the request (400 invalid_grant).")

    client = GmailClient(settings(), broken)
    with pytest.raises(GmailError) as exc:
        client.label_id("ASB notes")
    message = str(exc.value)
    assert "made-up-secret" not in message
    assert "made-up-refresh" not in message


def test_gmail_feed_can_return_duplicate_contract_notes_for_storage_to_deduplicate():
    class DuplicateClient:
        def label_id(self, name):
            return "Label_42"

        def message_ids(self, label_id):
            return iter(("copy-one", "copy-two"))

        def raw_message(self, message_id):
            return (FIXTURES / "asb-contract-note.eml").read_bytes()

    result = AsbGmailImporter(DuplicateClient(), "ASB notes").fetch()
    assert [trade.external_ref for trade in result.trades] == [
        "contract:40012345",
        "contract:40012345",
    ]


def test_save_refresh_token_refuses_existing_file_and_force_replaces_privately(tmp_path):
    from portfolio_digest.gmail import save_refresh_token

    path = tmp_path / ".env"
    path.write_text("MADE_UP_SETTING=preserve-me\n")
    with pytest.raises(GmailError, match="already exists"):
        save_refresh_token(path, "made-up-token")
    assert path.read_text() == "MADE_UP_SETTING=preserve-me\n"
    save_refresh_token(path, "made-up-token", force=True)
    assert path.read_text() == "GMAIL_REFRESH_TOKEN=made-up-token\n"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("dangling", [False, True])
def test_save_refresh_token_refuses_symlinks(tmp_path, force, dangling):
    from portfolio_digest.gmail import save_refresh_token

    target = tmp_path / "made-up-target"
    if not dangling:
        target.write_text("preserve-me")
    link = tmp_path / "token-link"
    link.symlink_to(target)
    with pytest.raises(GmailError):
        save_refresh_token(link, "made-up-token", force=force)
    assert link.is_symlink()
    assert not target.exists() if dangling else target.read_text() == "preserve-me"


def test_save_refresh_token_creates_private_file(tmp_path):
    from portfolio_digest.gmail import save_refresh_token

    path = tmp_path / "made-up-token-file"
    save_refresh_token(path, "made-up-token")
    assert path.read_text() == "GMAIL_REFRESH_TOKEN=made-up-token\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_authorise_cli_passes_force_and_names_env_variable_without_exposing_token(
    tmp_path, monkeypatch, capsys
):
    from portfolio_digest import cli
    from portfolio_digest.gmail import save_refresh_token

    path = tmp_path / "made-up-token-file"
    path.write_text("preserve-me")

    def fake_authorise(settings, output, *, force):
        save_refresh_token(output, "made-up-refresh-token", force=force)

    monkeypatch.setattr(cli, "get_gmail_settings", settings)
    monkeypatch.setattr(cli, "authorise", fake_authorise)
    args = ["gmail", "authorise", "--output", str(path)]
    assert cli.main(args) == 1
    assert path.read_text() == "preserve-me"
    assert "--force" in capsys.readouterr().err
    assert cli.main([*args, "--force"]) == 0
    output = capsys.readouterr()
    assert "GMAIL_REFRESH_TOKEN" in output.out and ".env" in output.out
    assert "made-up-refresh-token" not in output.out + output.err
