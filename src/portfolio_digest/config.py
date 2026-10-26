"""Settings, read from environment variables (or a `.env` file on the server).

Secrets never live in the repo. See `.env.example` for the full list.
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Full SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@db:5432/portfolio.
    # Kept as a secret so it is never printed in logs or error messages.
    database_url: SecretStr

    # Every value is reported in this currency.
    base_currency: str = "NZD"

    # Timezone used for schedules and "today".
    timezone: str = "Pacific/Auckland"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
