# backend/config.py

"""Application configuration loaded from environment variables.
Uses pydantic-settings BaseSettings for type‑conversion and defaults.
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str = Field(...)
    BOT_USERNAME: str = Field(...)
    ADMIN_ID: int = Field(...)
    WEBAPP_URL: str = Field(...)

    # Database URL (PostgreSQL async or SQLite async)
    DATABASE_URL: str = Field(...)

    # Misc
    LOG_LEVEL: str = Field("info")

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
