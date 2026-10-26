"""Mail delivery adapters. Provider errors are deliberately never echoed."""

import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal

import httpx
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from portfolio_digest.config import ConfigError


class MailError(Exception):
    """Safe delivery failure; provider messages can contain passwords or message bodies."""


class MailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    mailer: Literal["gmail", "postmark", "ses", "smtp"] = "gmail"
    mail_from: str = ""
    mail_owner: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_tls: Literal["starttls", "ssl"] = "starttls"
    smtp_username: SecretStr | None = None
    smtp_password: SecretStr | None = None
    gmail_smtp_username: SecretStr | None = None
    gmail_app_password: SecretStr | None = None
    postmark_server_token: SecretStr | None = None
    ses_smtp_username: SecretStr | None = None
    ses_smtp_password: SecretStr | None = None
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None
    aws_region: str = "ap-southeast-2"

    @field_validator(
        "smtp_username",
        "smtp_password",
        "gmail_smtp_username",
        "gmail_app_password",
        "postmark_server_token",
        "ses_smtp_username",
        "ses_smtp_password",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        mode="before",
    )
    @classmethod
    def empty_secret(cls, value):
        return None if value == "" else value


def mail_settings() -> MailSettings:
    try:
        return MailSettings()
    except ValidationError:
        raise ConfigError("Invalid mail setting. See .env.example.") from None


def address(value: str) -> str:
    # Deliberately accept mailbox addresses only, without display names or multiple recipients.
    if (
        not value
        or value.count("@") != 1
        or not all(value.split("@"))
        or any(c.isspace() for c in value)
        or any(c in value for c in ",;<>")
    ):
        raise ConfigError("Mail addresses must be single mailbox addresses without display names.")
    return value


def message(sender: str, recipients: list[str], subject: str, html: str, text: str) -> EmailMessage:
    result = EmailMessage()
    result["From"] = address(sender)
    # Bcc protects the recipient list; the actual envelope is passed separately.
    result["To"] = "undisclosed-recipients:;"
    for recipient in recipients:
        address(recipient)
    result["Subject"] = subject
    result.set_content(text)
    result.add_alternative(html, subtype="html")
    return result


def _secret(value: SecretStr | None, setting: str) -> str:
    if value is None or not value.get_secret_value():
        raise ConfigError(f"Missing setting: {setting}. See .env.example.")
    return value.get_secret_value()


class SmtpMailer:
    def __init__(self, settings: MailSettings):
        self.settings = settings

    def send(self, msg: EmailMessage, recipients: list[str]) -> None:
        s = self.settings
        if s.mailer == "gmail":
            host, port, tls = "smtp.gmail.com", 587, "starttls"
            username = _secret(s.gmail_smtp_username, "GMAIL_SMTP_USERNAME")
            password = _secret(s.gmail_app_password, "GMAIL_APP_PASSWORD")
        elif s.mailer == "ses":
            host, port, tls = f"email-smtp.{s.aws_region}.amazonaws.com", 587, "starttls"
            username = _secret(s.ses_smtp_username, "SES_SMTP_USERNAME")
            password = _secret(s.ses_smtp_password, "SES_SMTP_PASSWORD")
        else:
            host, port, tls = s.smtp_host, s.smtp_port, s.smtp_tls
            if not host:
                raise ConfigError("Missing setting: SMTP_HOST.")
            username = s.smtp_username.get_secret_value() if s.smtp_username else None
            password = _secret(s.smtp_password, "SMTP_PASSWORD") if username else None
        context = ssl.create_default_context()
        factory = smtplib.SMTP_SSL if tls == "ssl" else smtplib.SMTP
        kwargs = {"context": context} if tls == "ssl" else {}
        with factory(host, port, timeout=30, **kwargs) as client:
            if tls == "starttls":
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            if username:
                client.login(username, password)
            refused = client.send_message(msg, from_addr=str(msg["From"]), to_addrs=recipients)
            if refused:
                raise MailError("Mail delivery failed: at least one recipient was refused.")


class PostmarkMailer:
    def __init__(self, settings: MailSettings):
        self.settings = settings

    def send(self, msg: EmailMessage, recipients: list[str]) -> None:
        token = _secret(self.settings.postmark_server_token, "POSTMARK_SERVER_TOKEN")
        with httpx.Client(timeout=30) as client:
            response = client.post(
                "https://api.postmarkapp.com/email",
                headers={"X-Postmark-Server-Token": token},
                json={
                    "From": str(msg["From"]),
                    "To": recipients[0],
                    "Bcc": ",".join(recipients[1:]),
                    "Subject": str(msg["Subject"]),
                    "HtmlBody": msg.get_body(preferencelist=("html",)).get_content(),
                    "TextBody": msg.get_body(preferencelist=("plain",)).get_content(),
                },
            )
            response.raise_for_status()
            if response.json().get("ErrorCode") != 0:
                raise MailError("Mail delivery failed: Postmark rejected the message.")


class SesMailer:
    def __init__(self, settings: MailSettings):
        self.settings = settings

    def send(self, msg: EmailMessage, recipients: list[str]) -> None:
        import boto3

        s = self.settings
        client = boto3.client(
            "ses",
            region_name=s.aws_region,
            aws_access_key_id=_secret(s.aws_access_key_id, "AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=_secret(s.aws_secret_access_key, "AWS_SECRET_ACCESS_KEY"),
            aws_session_token=s.aws_session_token.get_secret_value()
            if s.aws_session_token
            else None,
        )
        try:
            client.send_raw_email(
                Source=str(msg["From"]),
                Destinations=recipients,
                RawMessage={"Data": msg.as_bytes()},
            )
        finally:
            client.close()


def send(settings: MailSettings, msg: EmailMessage, recipients: list[str]) -> None:
    if not recipients:
        raise ConfigError("Set MAIL_OWNER or configure report recipients.")
    if settings.mailer == "postmark":
        mailer = PostmarkMailer(settings)
    elif settings.mailer == "ses" and settings.aws_access_key_id:
        mailer = SesMailer(settings)
    else:
        mailer = SmtpMailer(settings)
    try:
        mailer.send(msg, recipients)
    except ConfigError:
        raise
    except Exception:
        # Never include provider exception text, response bodies, SMTP credentials or URLs.
        raise MailError("Mail delivery failed. Check mail settings and provider status.") from None
