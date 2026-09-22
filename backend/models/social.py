# backend/models/social.py

"""Models for social features: online presence, invites, chat, game history."""

from sqlalchemy import Column, BigInteger, String, Integer, DateTime, JSON
from sqlalchemy.orm import declarative_base
from .mutable import JSONList, JSONDict
from datetime import datetime

Base = declarative_base()


class OnlineUser(Base):
    __tablename__ = "online_users"

    id = Column(BigInteger, primary_key=True, index=True)  # Telegram user_id
    username = Column(String, nullable=True, index=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    # In-app nickname (user can change it; preferred over Telegram names)
    nickname = Column(String(24), nullable=True)
    # "main_owner" | "owner" | "admin" | "deputy" | None(player)
    role = Column(String(16), nullable=True)
    # 1 = blocked: cannot chat, invite or join rooms
    blocked = Column(Integer, default=0, nullable=False)
    # Optional block expiry (muddatli blok); NULL + blocked=1 => permanent
    blocked_until = Column(DateTime, nullable=True)
    # Optional reason shown to the blocked user
    block_reason = Column(String(140), nullable=True)
    last_online = Column(DateTime, default=datetime.utcnow, index=True)

    # Stats (updated when a game result is recorded)
    games_played = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    xp = Column(Integer, default=0)
    level = Column(Integer, default=1)

    # Pending/processed in-app invites stored as JSON list
    invites = Column(JSONList, default=list)


class GameRecord(Base):
    __tablename__ = "game_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_code = Column(String, nullable=False, index=True)
    winner_id = Column(BigInteger, nullable=True, index=True)
    winner_name = Column(String, nullable=True)
    players = Column(JSONList, default=list)  # [{id, name}]
    finished_at = Column(DateTime, default=datetime.utcnow, index=True)


class ModLogEntry(Base):
    __tablename__ = "mod_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_id = Column(BigInteger, nullable=True, index=True)
    actor_name = Column(String, nullable=True)
    action = Column(String(24), nullable=False, index=True)   # block | unblock | role | delete | cleanup | system
    target_id = Column(BigInteger, nullable=True, index=True)
    text = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room = Column(String, default="global", index=True)  # "global" for now
    user_id = Column(BigInteger, nullable=False)
    name = Column(String, nullable=True)
    text = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Friendship(Base):
    __tablename__ = "friendships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)      # who sent the request
    friend_id = Column(BigInteger, nullable=False, index=True)    # who receives it
    # "pending" | "accepted"
    status = Column(String(16), default="pending", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
