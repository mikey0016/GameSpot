# backend/database.py

"""Async database engine and session maker.
Supports PostgreSQL via asyncpg and SQLite fallback.
"""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from .config import settings

# Create async engine
engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)

# Session maker for dependency injection
async_session_maker = async_sessionmaker(bind=engine, expire_on_commit=False)

# Optional helper to create tables (run at startup)
async def init_db():
    """Create all tables defined in model Base metadata.
    Each model module defines its own declarative Base; we import them
    and call ``metadata.create_all`` on the async engine.
    """
    from .models import UserBase, RoomBase, GameBase, SocialBase
    async with engine.begin() as conn:
        await conn.run_sync(UserBase.metadata.create_all)
        await conn.run_sync(RoomBase.metadata.create_all)
        await conn.run_sync(GameBase.metadata.create_all)
        await conn.run_sync(SocialBase.metadata.create_all)

    # Lightweight migration: add columns that appeared after the tables were
    # first created (create_all does not alter existing tables).
    try:
        from sqlalchemy import text
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS nickname VARCHAR(24)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS role VARCHAR(16)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS blocked INTEGER DEFAULT 0"
            ))
    except Exception:
        pass  # SQLite or already migrated

