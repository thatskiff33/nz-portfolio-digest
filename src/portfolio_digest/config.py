"""Settings, read from environment variables (or a `.env` file on the server).

Secrets never live in the repo. See `.env.example` for the full list.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from portfolio_digest.validation import currency_code


class ConfigError(Exception):
    """Settings are missing or invalid. The message names the setting but never its value."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Full SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@db:5432/portfolio.
    # Kept as a secret so it is never printed in logs or error messages.
    database_url: SecretStr

    # Every value is reported in this currency, and new portfolios use it by default.
    base_currency: str = "NZD"

    # Timezone used for schedules and "today".
    timezone: str = "Pacific/Auckland"

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
