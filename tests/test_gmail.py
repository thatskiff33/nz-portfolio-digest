import base64
import os
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from pydantic import SecretStr

from portfolio_digest.config import GmailSettings
from portfolio_digest.gmail import SCOPE, AsbGmailImporter, GmailClient, GmailError

FIXTURES = Path(__file__).parent / "fixtures"


def test_compose_passes_optional_gmail_settings_to_the_import_container():
    compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()
    app_environment = compose.split("environment: &app_environment\n", 1)[1].split(
        "    depends_on:", 1
    )[0]
    for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_LABEL"):
        assert f"{name}: ${{{name}:-}}" in app_environment


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
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("dangling", [False, True])
def test_save_refresh_token_refuses_symlinks(tmp_path, force, dangling):
    from portfolio_digest.gmail import save_refresh_token

    target = tmp_path / "made-up-target"
    if not dangling:
        target.write_text("preserve-me")
    link = tmp_path / "token-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("This platform cannot create symlinks.")
    with pytest.raises(GmailError):
        save_refresh_token(link, "made-up-token", force=force)
    assert link.is_symlink()
    assert not target.exists() if dangling else target.read_text() == "preserve-me"


def test_save_refresh_token_creates_private_file(tmp_path):
    from portfolio_digest.gmail import save_refresh_token

    path = tmp_path / "made-up-token-file"
    save_refresh_token(path, "made-up-token")
    assert path.read_text() == "GMAIL_REFRESH_TOKEN=made-up-token\n"
    if os.name != "nt":
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


@pytest.mark.parametrize(
    "old,new,reason",
    [
        (b"Security code: FPH", b"Security code: FP\x00H", "control characters"),
        (
            b"Security name: Example Healthcare Limited",
            b"Security name: Example\x00Healthcare",
            "control characters",
        ),
        (b"charset=utf-8", b'charset="utf-8\x00"', "encoding we cannot read"),
        (None, None, "could not read this message"),
        (b"Market: NZX", b"Market: LSE", "exchange"),
        (
            b"Brokerage: NZD 30.00\nGST: NZD 4.50",
            b"Brokerage: NZD 99999999999999\nGST: NZD 1",
            "Fees is outside the supported money range",
        ),
        (
            b"Security name: Example Healthcare Limited",
            b"Security name: " + b"A" * 300,
            "Security name must be 255 characters or fewer",
        ),
        (b"Quantity: 100\n", b"", "missing: quantity"),
        (b"Brokerage: NZD", b"Brokerage: AUD", "expected NZD"),
        (b"charset=utf-8", b"charset=made-up-encoding", "encoding we cannot read"),
    ],
)
def test_unreadable_gmail_notes_are_skipped_without_blocking_later_messages(
    monkeypatch, old, new, reason
):
    original = (FIXTURES / "asb-contract-note.eml").read_bytes()

    class Client:
        def label_id(self, name):
            return "made-up-label"

        def message_ids(self, label_id):
            return iter(("unreadable", "valid"))

        def raw_message(self, message_id):
            if message_id == "unreadable":
                return original.replace(old, new) if old is not None else b"made-up-unexpected"
            return original

    from portfolio_digest import gmail

    parse = gmail.parse_contract_note

    def parse_with_unexpected_error(raw, where):
        if raw == b"made-up-unexpected":
            raise RuntimeError("made-up-private-message-content")
        return parse(raw, where)

    monkeypatch.setattr(gmail, "parse_contract_note", parse_with_unexpected_error)
    importer = AsbGmailImporter(Client(), "ASB notes")
    for _ in range(2):
        result = importer.fetch()
        assert not result.errors
        assert [trade.external_ref for trade in result.trades] == ["contract:40012345"]
        assert len(result.skipped) == 1
        assert result.skipped[0].where == "message unreadable"
        assert reason in result.skipped[0].message
        assert "made-up-private-message-content" not in str(result.skipped[0])


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_gmail_message_interrupts_propagate(failure):
    class Client:
        def label_id(self, name):
            return "made-up-label"

        def message_ids(self, label_id):
            return iter(("interrupted",))

        def raw_message(self, message_id):
            raise failure()

    with pytest.raises(failure):
        AsbGmailImporter(Client(), "ASB notes").fetch()


def test_gmail_fetch_failure_after_valid_message_propagates():
    class Client:
        def label_id(self, name):
            return "made-up-label"

        def message_ids(self, label_id):
            return iter(("valid", "network-failure"))

        def raw_message(self, message_id):
            if message_id == "network-failure":
                raise GmailError("Gmail request failed.")
            return (FIXTURES / "asb-contract-note.eml").read_bytes()

    with pytest.raises(GmailError, match="request failed"):
        AsbGmailImporter(Client(), "ASB notes").fetch()


def test_save_refresh_token_without_nofollow(tmp_path, monkeypatch):
    from portfolio_digest.gmail import save_refresh_token

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    path = tmp_path / "made-up-token-file"
    save_refresh_token(path, "invented-refresh")
    assert path.read_text() == "GMAIL_REFRESH_TOKEN=invented-refresh\n"


@pytest.mark.parametrize("stage", ["write", "replace"])
def test_failed_force_save_preserves_existing_file(tmp_path, monkeypatch, stage):
    from portfolio_digest.gmail import save_refresh_token

    path = tmp_path / "made-up-token-file"
    path.write_text("preserve-me")

    def fail(*args, **kwargs):
        raise OSError("invented write failure")

    monkeypatch.setattr(os, "fdopen" if stage == "write" else "replace", fail)
    with pytest.raises(GmailError, match="Cannot save"):
        save_refresh_token(path, "invented-refresh", force=True)
    assert path.read_text() == "preserve-me"
    assert list(tmp_path.iterdir()) == [path]


def test_save_without_force_preserves_concurrent_output(tmp_path, monkeypatch):
    from portfolio_digest import gmail

    path = tmp_path / "made-up-token-file"
    check_output = gmail._check_token_output
    checks = 0

    def create_after_final_check(output, *, force):
        nonlocal checks
        check_output(output, force=force)
        checks += 1
        if checks == 2:
            output.write_text("preserve-concurrent-token")

    monkeypatch.setattr(gmail, "_check_token_output", create_after_final_check)
    with pytest.raises(GmailError, match="already exists"):
        gmail.save_refresh_token(path, "invented-refresh")
    assert path.read_text() == "preserve-concurrent-token"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "kind,force",
    [
        ("existing", False),
        ("symlink", False),
        ("symlink", True),
        ("dangling", False),
        ("dangling", True),
    ],
)
def test_authorise_checks_output_before_sign_in(tmp_path, monkeypatch, kind, force):
    from portfolio_digest import gmail

    output = tmp_path / "made-up-output"
    if kind == "existing":
        output.write_text("preserve-me")
    else:
        target = tmp_path / "made-up-target"
        if kind == "symlink":
            target.write_text("preserve-me")
        try:
            output.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("This platform cannot create symlinks.")

    def unexpected(*args, **kwargs):
        pytest.fail("Sign-in must not start for a refused output path.")

    monkeypatch.setattr(gmail, "_Server", unexpected)
    with pytest.raises(GmailError, match="already exists|symlink"):
        gmail.authorise(settings(), output, show=unexpected, http=unexpected, force=force)


@pytest.mark.parametrize("destination", ["http://other.invalid/", "https://other.invalid/"])
def test_gmail_refuses_redirects_without_forwarding_credentials(monkeypatch, destination):
    from email.message import Message
    from io import BytesIO
    from urllib.request import HTTPSHandler, build_opener
    from urllib.response import addinfourl

    from portfolio_digest import gmail

    calls = []

    class RedirectingGoogle(HTTPSHandler):
        def https_open(self, request):
            calls.append(request)
            headers = Message()
            headers["Location"] = destination
            response = addinfourl(BytesIO(b"{}"), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    def fake_opener(*handlers):
        return build_opener(RedirectingGoogle(), *handlers)

    monkeypatch.setattr(gmail, "urlopen", fake_opener().open, raising=False)
    monkeypatch.setattr(gmail, "build_opener", fake_opener, raising=False)
    with pytest.raises(GmailError, match="302"):
        gmail.urllib_http(
            "GET", gmail.API_URL + "/labels", {"Authorization": "Bearer invented-token"}, None
        )
    assert len(calls) == 1
