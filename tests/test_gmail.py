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
