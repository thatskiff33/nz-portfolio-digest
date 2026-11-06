"""Settings, read from environment variables (or a `.env` file on the server).

Secrets never live in the repo. See `.env.example` for the full list.
"""

import json
import re
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from portfolio_digest.prices.sources import FX_SOURCES, PRICE_SOURCES
from portfolio_digest.validation import currency_code

# One DNS label, then optional dotted labels. No wildcard, scheme, port or path.
_EXACT_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


class ConfigError(Exception):
    """Settings are missing or invalid. The message names the setting but never its value."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Full SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@db:5432/portfolio.
    # Kept as a secret so it is never printed in logs or error messages.
    database_url: SecretStr

    akahu_app_token: SecretStr | None = None
    akahu_user_token: SecretStr | None = None
    # Optional JSON array of account IDs. Empty selects all investment accounts. Kept as
    # raw text and parsed only by `akahu sync`, so a typo cannot break other commands.
    akahu_account_ids: str = ""
    uptime_kuma_push_url: SecretStr | None = None

    # Every value is reported in this currency, and new portfolios use it by default.
    base_currency: str = "NZD"

    # Timezone used for "today".
    timezone: str = "Pacific/Auckland"

    # Where daily prices come from. Only "yahoo" for now; a paid source slots in later.
    price_source: str = "yahoo"

    # Where exchange rates come from, tried in order until one works.
    fx_sources: str = "yahoo,frankfurter"

    # Dashboard only. Empty is allowed here so the CLI still starts; the web process
    # rejects a missing or short password when it builds sessions.
    dashboard_password: SecretStr | None = None
    dashboard_bind: str = "127.0.0.1"
    # Pydantic accepts 1/yes/true/on and 0/no/false/off from the environment.
    dashboard_secure_cookie: bool = False
    # Optional exact hostnames (for example a Tailscale Serve name), comma-separated.
    dashboard_hostnames: str = ""

    @field_validator("base_currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        return currency_code(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("not a known timezone such as Pacific/Auckland") from None
        return value

    @field_validator("price_source")
    @classmethod
    def _check_price_source(cls, value: str) -> str:
        name = value.strip().lower()
        if name not in PRICE_SOURCES:
            raise ValueError(f"must be one of {', '.join(PRICE_SOURCES)}")
        return name

    @field_validator("fx_sources")
    @classmethod
    def _check_fx_sources(cls, value: str) -> str:
        names = [name.strip().lower() for name in value.split(",") if name.strip()]
        if not names or any(name not in FX_SOURCES for name in names):
            raise ValueError(f"must list one or more of {', '.join(FX_SOURCES)}")
        return ",".join(names)

    @field_validator("dashboard_hostnames")
    @classmethod
    def _check_dashboard_hostnames(cls, value: str) -> str:
        names: list[str] = []
        for raw in value.split(","):
            name = raw.strip()
            if not name:
                continue
            if (
                any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in name)
                or any(token in name.lower() for token in ("*", "://", "/", "\\", ":"))
                or _EXACT_HOSTNAME.fullmatch(name) is None
            ):
                raise ValueError(
                    "must be comma-separated exact hostnames, "
                    "without a scheme, port, path or wildcard"
                )
            lowered = name.lower()
            if lowered not in names:
                names.append(lowered)
        return ",".join(names)

    @property
    def fx_source_names(self) -> list[str]:
        return self.fx_sources.split(",")

    @property
    def dashboard_hostname_list(self) -> list[str]:
        return [name for name in self.dashboard_hostnames.split(",") if name]


def akahu_account_ids(raw: str) -> list[str]:
    """Parse AKAHU_ACCOUNT_IDS. The error never echoes the value."""
    if not raw.strip():
        return []
    try:
        ids = json.loads(raw)
    except ValueError:
        ids = None
    if (
        not isinstance(ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in ids)
        or len({item.strip() for item in ids}) != len(ids)
    ):
        raise ConfigError(
            'AKAHU_ACCOUNT_IDS must be a JSON array of distinct account IDs, e.g. ["acc_example"].'
        )
    return [item.strip() for item in ids]


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # pydantic's own message echoes the values it was given, which can include secrets.
        names = sorted({str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]})
        raise ConfigError(
            f"Missing or invalid setting: {', '.join(names)}. See .env.example."
        ) from None


class GmailSettings(BaseSettings):
    """OAuth details for reading ASB contract-note emails from one Gmail label.

    Kept apart from `Settings` so `gmail authorise` can run on a laptop with no database.
    """

    model_config = SettingsConfigDict(
        env_prefix="GMAIL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    client_id: str | None = None
    client_secret: SecretStr | None = None
    # Written by `portfolio-digest gmail authorise`. Read-only access to the mailbox.
    refresh_token: SecretStr | None = None
    # Only emails with this label are read, e.g. "ASB contract notes".
    label: str | None = None

    def require(self, *names: str) -> None:
        missing = [f"GMAIL_{n.upper()}" for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(f"Missing setting: {', '.join(missing)}. See .env.example.")


@lru_cache
def get_gmail_settings() -> GmailSettings:
    try:
        return GmailSettings()
    except ValidationError as exc:
        names = sorted(
            {f"GMAIL_{error['loc'][0]}".upper() for error in exc.errors() if error["loc"]}
        )
        raise ConfigError(f"Invalid setting: {', '.join(names)}. See .env.example.") from None


class ReportSettings(BaseSettings):
    """Private report overrides, usable without database or mail credentials."""

    model_config = SettingsConfigDict(
        env_prefix="REPORT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    recipients: str = ""
    kuma_push_url: SecretStr | None = None


def get_report_settings() -> ReportSettings:
    try:
        return ReportSettings()
    except ValidationError as exc:
        names = sorted(
            {f"REPORT_{error['loc'][0]}".upper() for error in exc.errors() if error["loc"]}
        )
        raise ConfigError(f"Invalid setting: {', '.join(names)}. See .env.example.") from None
