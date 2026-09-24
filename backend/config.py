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

    # Obuna sharti bo'lgan kanal (bot tekshiruvi uchun). Bo'sh bo'lsa tekshiruv o'tkazib yuboriladi.
    CHANNEL_URL: str = Field("https://t.me/gamespotofficial")
    # Ixtiyoriy: privat kanal uchun raqamli ID (-100...). Bo'sh bo'lsa CHANNEL_URL'dan @username olinadi.
    CHANNEL_ID: str = Field("")

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
