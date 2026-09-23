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
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS blocked_until TIMESTAMP"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS block_reason VARCHAR(140)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS muted INTEGER DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS muted_until TIMESTAMP"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS warning VARCHAR(220)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS coins INTEGER DEFAULT 0"
            ))
            # rooms: columns added after the first release (join tracking + game ref)
            await conn.execute(text(
                "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS player_ids JSON DEFAULT '[]'"
            ))
            await conn.execute(text(
                "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS game_id VARCHAR"
            ))
            await conn.execute(text(
                "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS password VARCHAR(24)"
            ))
            # friendships: old servers created status as a native enum 'friendstatus';
            # the code expects VARCHAR — convert once (noop when already varchar)
            await conn.execute(text(
                "DO $$ BEGIN "
                "ALTER TABLE friendships ALTER COLUMN status TYPE VARCHAR USING status::text; "
                "EXCEPTION WHEN others THEN NULL; END $$;"
            ))
            # friendships: drop legacy FK constraints (they reference the OLD app's
            # users table, so new Telegram users violate them) and widen ids to BIGINT
            await conn.execute(text(
                "ALTER TABLE friendships DROP CONSTRAINT IF EXISTS friendships_user_id_fkey"
            ))
            await conn.execute(text(
                "ALTER TABLE friendships DROP CONSTRAINT IF EXISTS friendships_friend_id_fkey"
            ))
            await conn.execute(text(
                "ALTER TABLE friendships ALTER COLUMN user_id TYPE BIGINT"
            ))
            await conn.execute(text(
                "ALTER TABLE friendships ALTER COLUMN friend_id TYPE BIGINT"
            ))
            # Qiziqarli funksiyalar ustunlari
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS daily_last TIMESTAMP"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS daily_streak INTEGER DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS achievements JSON DEFAULT '[]'"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS ref_code VARCHAR(10)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS referred_by BIGINT"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS ref_count INTEGER DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS skin_board VARCHAR(24)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS skin_frame VARCHAR(24)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS skin_badge VARCHAR(24)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS skin_avatar VARCHAR(24)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS skin_banner VARCHAR(24)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS skin_bg VARCHAR(24)"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS blitz_wins INTEGER DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS likes INTEGER DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS liked_by JSON DEFAULT '[]'"
            ))
            await conn.execute(text(
                "ALTER TABLE online_users ADD COLUMN IF NOT EXISTS badges JSON DEFAULT '[]'"
            ))
            # turnirs: bracket o'yinlari
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS tournaments (id SERIAL PRIMARY KEY, code VARCHAR(8) UNIQUE, "
                "host_id BIGINT, entry_fee INTEGER DEFAULT 0, prize INTEGER DEFAULT 0, status VARCHAR(16) DEFAULT 'open', "
                "players JSON DEFAULT '[]', matches JSON DEFAULT '[]', winner_id BIGINT, created_at TIMESTAMP, finished_at TIMESTAMP)"
            ))
            # game_records: to'liq o'yin ma'lumoti (detail modal)
            await conn.execute(text(
                "ALTER TABLE game_records ADD COLUMN IF NOT EXISTS started_at TIMESTAMP"
            ))
            await conn.execute(text(
                "ALTER TABLE game_records ADD COLUMN IF NOT EXISTS duration_sec INTEGER"
            ))
            await conn.execute(text(
                "ALTER TABLE game_records ADD COLUMN IF NOT EXISTS draw_count INTEGER"
            ))
            await conn.execute(text(
                "ALTER TABLE game_records ADD COLUMN IF NOT EXISTS turn_count INTEGER"
            ))
            # Rooms: entry_fee / prize for rejimli xonalar
            await conn.execute(text(
                "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS entry_fee INTEGER DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS prize INTEGER DEFAULT 0"
            ))
            # Backfill: old rows have NULL player_ids; seed with host id so they stay joinable
            await conn.execute(text(
                "UPDATE rooms SET player_ids = json_build_array(host_id) WHERE player_ids IS NULL"
            ))
    except Exception:
        pass  # SQLite or already migrated

