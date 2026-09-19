# backend/config.py

"""Application configuration loaded from environment variables.
Uses pydantic BaseSettings for type‑conversion and defaults.
"""

from pydantic import BaseSettings, Field, validator
from typing import Optional

class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str = Field(..., env="BOT_TOKEN")
    BOT_USERNAME: str = Field(..., env="BOT_USERNAME")
    ADMIN_ID: int = Field(..., env="ADMIN_ID")
    WEBAPP_URL: str = Field(..., env="WEBAPP_URL")

    # Database URL (PostgreSQL async or SQLite async)
    DATABASE_URL: str = Field(..., env="DATABASE_URL")

    # Misc
    LOG_LEVEL: str = Field("info", env="LOG_LEVEL")

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()
