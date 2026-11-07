"""Mail provider contracts without network connections."""

# All credentials below are deliberately fictitious test sentinels.
# ruff: noqa: S105, S106

from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import SecretStr

from portfolio_digest import mailers
from portfolio_digest.config import ConfigError


@pytest.fixture
def msg():
    return mailers.message(
        "digest@example.invalid", ["reader@example.invalid"], "Digest", "<p>Hello</p>", "Hello"
    )


@pytest.mark.parametrize(
    "provider,implicit,host",
    [
        ("gmail", False, "smtp.gmail.com"),
        ("smtp", False, "mail.example.invalid"),
        ("smtp", True, "mail.example.invalid"),
        ("ses", False, "email-smtp.ap-southeast-2.amazonaws.com"),
    ],
)
def test_smtp_providers_encrypt_then_authenticate_and_send_both_parts(
    monkeypatch,
    msg,
    provider,
    implicit,
    host,
):
    events = []
    client = MagicMock()
    client.__enter__.return_value = client
    client.send_message.return_value = {}
    client.starttls.side_effect = lambda **kw: events.append("tls")
    client.login.side_effect = lambda *a: events.append("login")
    client.send_message.side_effect = lambda *a, **kw: events.append("send") or {}
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(mailers.smtplib, "SMTP_SSL" if implicit else "SMTP", factory)
    settings = mailers.MailSettings(
        _env_file=None,
        mailer=provider,
        smtp_host=host,
        smtp_tls="ssl" if implicit else "starttls",
        smtp_port=465 if implicit else 587,
        smtp_username="user",
        smtp_password="test-only-password",
        gmail_smtp_username="gmail-user",
        gmail_app_password="test-only-password",
        ses_smtp_username="ses-user",
        ses_smtp_password="test-only-password",
    )
    mailers.send(settings, msg, ["reader@example.invalid"])
    assert factory.call_args.args[:2] == (host, 465 if implicit else 587)
    assert events == (["login", "send"] if implicit else ["tls", "login", "send"])
    assert client.send_message.call_args.kwargs["to_addrs"] == ["reader@example.invalid"]
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "Hello"
    assert msg.get_body(preferencelist=("html",)).get_content().strip() == "<p>Hello</p>"
    assert "Bcc" not in msg


@pytest.mark.parametrize("with_attachment", [False, True])
def test_postmark_http_contract_and_token_header(monkeypatch, msg, with_attachment):
    if with_attachment:
        msg.add_attachment(
            b"Holding,Net\r\nNZX:EXAMPLE,32\r\n",
            maintype="text",
            subtype="csv",
            filename="year-end-2026.csv",
        )
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ErrorCode": 0, "MessageID": "fake-id"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(mailers.httpx, "Client", lambda **kw: client)
    mailers.send(
        mailers.MailSettings(
            _env_file=None, mailer="postmark", postmark_server_token="test-only-token"
        ),
        msg,
        ["reader@example.invalid", "second@example.invalid"],
    )
    import json

    payloads = [json.loads(request.content) for request in requests]
    # Each recipient gets their own message, so no address is disclosed to another.
    assert [payload["To"] for payload in payloads] == [
        "reader@example.invalid",
        "second@example.invalid",
    ]
    assert not any("Bcc" in payload or "Cc" in payload for payload in payloads)
    assert all(request.url == "https://api.postmarkapp.com/email" for request in requests)
    assert all(r.headers["X-Postmark-Server-Token"] == "test-only-token" for r in requests)
    assert payloads[0] == {**payloads[1], "To": "reader@example.invalid"}
    payload = payloads[0]
    assert payload["TextBody"].strip() == "Hello"
    assert payload["HtmlBody"].strip() == "<p>Hello</p>"
    if with_attachment:
        import base64

        attachment = payload["Attachments"][0]
        assert attachment["Name"] == "year-end-2026.csv"
        assert attachment["ContentType"] == "text/csv"
        assert base64.b64decode(attachment["Content"]) == b"Holding,Net\r\nNZX:EXAMPLE,32\r\n"
    else:
        assert "Attachments" not in payload


def test_ses_api_sends_raw_multipart_and_explicit_credentials(monkeypatch, msg):
    import boto3

    client = MagicMock()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(boto3, "client", factory)
    mailers.send(
        mailers.MailSettings(
            _env_file=None,
            mailer="ses",
            aws_access_key_id="fake-key",
            aws_secret_access_key="test-only-secret",
            aws_session_token="test-only-session",
        ),
        msg,
        ["reader@example.invalid"],
    )
    assert factory.call_args.kwargs["aws_secret_access_key"] == "test-only-secret"
    kwargs = client.send_raw_email.call_args.kwargs
    assert kwargs["Destinations"] == ["reader@example.invalid"]
    assert b"multipart/alternative" in kwargs["RawMessage"]["Data"]
    client.close.assert_called_once()


@pytest.mark.parametrize("provider", ["gmail", "smtp", "postmark", "ses"])
def test_provider_exceptions_never_expose_credentials_or_message_body(
    monkeypatch, caplog, msg, provider
):
    def fail(*a):
        raise RuntimeError("test-only-password private-portfolio private-token")

    for cls in (mailers.SmtpMailer, mailers.PostmarkMailer, mailers.SesMailer):
        monkeypatch.setattr(cls, "send", fail)
    settings = mailers.MailSettings(
        _env_file=None,
        mailer=provider,
        aws_access_key_id="test-only-key" if provider == "ses" else None,
    )
    with pytest.raises(mailers.MailError) as caught:
        mailers.send(settings, msg, ["reader@example.invalid"])
    output = str(caught.value) + caplog.text
    assert "test-only" not in output and "private-portfolio" not in output
    assert caught.value.__suppress_context__


def test_postmark_rejected_message_is_a_delivery_failure(monkeypatch, msg):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ErrorCode": 300, "Message": "private-token"})
        )
    )
    monkeypatch.setattr(mailers.httpx, "Client", lambda **kw: client)
    with pytest.raises(mailers.MailError, match="Mail delivery failed") as caught:
        mailers.send(
            mailers.MailSettings(
                _env_file=None, mailer="postmark", postmark_server_token="test-only-token"
            ),
            msg,
            ["reader@example.invalid"],
        )
    assert "private-token" not in str(caught.value)


def test_partial_smtp_refusal_is_a_failure(monkeypatch, msg):
    client = MagicMock()
    client.__enter__.return_value = client
    client.send_message.return_value = {"reader@example.invalid": (550, "private-token")}
    monkeypatch.setattr(mailers.smtplib, "SMTP", MagicMock(return_value=client))
    with pytest.raises(mailers.MailError):
        mailers.send(
            mailers.MailSettings(_env_file=None, mailer="smtp", smtp_host="example.invalid"),
            msg,
            ["reader@example.invalid"],
        )


def test_invalid_settings_and_headers_do_not_echo_secrets(monkeypatch):
    monkeypatch.setenv("MAILER", "private-token")
    with pytest.raises(ConfigError) as caught:
        mailers.mail_settings()
    assert "private-token" not in str(caught.value)
    with pytest.raises(ConfigError):
        mailers.message("sender@example.invalid\nBcc: hidden@example.invalid", [], "Hi", "", "")
    assert "private-token" not in repr(SecretStr("private-token"))


def test_empty_compose_api_keys_do_not_select_ses_api(monkeypatch, msg):
    calls = []
    monkeypatch.setattr(mailers.SmtpMailer, "send", lambda *a: calls.append("smtp"))
    monkeypatch.setattr(
        mailers.SesMailer, "send", lambda *a: pytest.fail("API selected for empty keys")
    )
    settings = mailers.MailSettings(
        _env_file=None,
        mailer="ses",
        aws_access_key_id="",
        aws_secret_access_key="",
        aws_session_token="",
    )
    mailers.send(settings, msg, ["reader@example.invalid"])
    assert calls == ["smtp"]


@pytest.mark.parametrize("failure", ["rejection", "http", "timeout", "json", "shape"])
@pytest.mark.parametrize("failed_index", [0, 1])
def test_postmark_attempts_every_recipient_and_retries_only_failures(
    monkeypatch, msg, failure, failed_index
):
    import json

    from portfolio_digest.delivery import RecipientProgress, track_recipients

    recipients = ["first@example.invalid", "second@example.invalid"]
    attempted = []
    rejecting = True

    def handle(request):
        recipient = json.loads(request.content)["To"]
        attempted.append(recipient)
        if rejecting and recipient == recipients[failed_index]:
            if failure == "timeout":
                raise httpx.ReadTimeout("private-token", request=request)
            if failure == "http":
                return httpx.Response(422, json={"Message": "private-token"})
            if failure == "json":
                return httpx.Response(200, text="private-token")
            if failure == "shape":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"ErrorCode": 406, "Message": "private-token"})
        return httpx.Response(200, json={"ErrorCode": 0})

    # Each send constructs a fresh adapter and HTTP client, as scheduler retries do.
    factory = httpx.Client
    monkeypatch.setattr(
        mailers.httpx, "Client", lambda **kw: factory(transport=httpx.MockTransport(handle))
    )
    settings = mailers.MailSettings(
        _env_file=None, mailer="postmark", postmark_server_token="test-only-token"
    )
    saved = []
    progress = RecipientProgress(set(), lambda: saved.append(set(progress.delivered)))
    with track_recipients(progress):
        for _ in range(2):
            with pytest.raises(mailers.MailError) as error:
                mailers.send(settings, msg, recipients)
            assert str(error.value) == "Mail delivery failed: 1 recipient(s) failed."
        rejecting = False
        mailers.send(settings, msg, recipients)
    assert attempted == recipients + [recipients[failed_index]] * 2
    assert len(saved) == 2
    assert all("@" not in key for key in progress.delivered)


def test_postmark_counts_all_failed_recipients(monkeypatch, msg):
    factory = httpx.Client
    monkeypatch.setattr(
        mailers.httpx,
        "Client",
        lambda **kw: factory(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ErrorCode": 406}))
        ),
    )
    with pytest.raises(
        mailers.MailError, match=r"^Mail delivery failed: 2 recipient\(s\) failed\.$"
    ):
        mailers.send(
            mailers.MailSettings(
                _env_file=None, mailer="postmark", postmark_server_token="test-only-token"
            ),
            msg,
            ["first@example.invalid", "second@example.invalid"],
        )
