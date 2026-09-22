# backend/models/social.py

"""Models for social features: online presence, invites, chat, game history."""

from ..tz import now_local
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
    # 1 = muted: cannot chat (global + rooms) but can play
    muted = Column(Integer, default=0, nullable=False)
    # Optional mute expiry
    muted_until = Column(DateTime, nullable=True)
    # Optional warning text shown to the user (player sees it until dismissed)
    warning = Column(String(220), nullable=True)
    last_online = Column(DateTime, default=now_local, index=True)

    # Stats (updated when a game result is recorded)
    games_played = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    # Coin currency (game rewards + owner grants)
    coins = Column(Integer, default=0, nullable=False)
    xp = Column(Integer, default=0)
    level = Column(Integer, default=1)

    # Pending/processed in-app invites stored as JSON list
    invites = Column(JSONList, default=list)

    # ===== 🆕 Qiziqarli funksiyalar =====
    # Kunlik bonus: streak
    daily_last = Column(DateTime, nullable=True)      # oxirgi bonus olingan kun
    daily_streak = Column(Integer, default=0, nullable=False)
    # Yutuqlar: JSON list [{id, name, icon, at}]
    achievements = Column(JSONList, default=list)
    # Referal: kimni taklif qildi
    ref_code = Column(String(10), nullable=True, index=True)
    referred_by = Column(BigInteger, nullable=True)
    ref_count = Column(Integer, default=0, nullable=False)
    # Skin: kiygan skin id (do'kon katalogi STATIK_FRONTEND'da)
    skin_board = Column(String(24), nullable=True)    # stol/doska skini
    skin_frame = Column(String(24), nullable=True)    # avatar ramkasi
    # Blitz hisoblagichlari
    blitz_wins = Column(Integer, default=0, nullable=False)
    # Do'stona so'rov + achievements unlock broadcast uchun
    badges = Column(JSONList, default=list)  # deprecated, achievements ishlatiladi


class GameRecord(Base):
    __tablename__ = "game_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_code = Column(String, nullable=False, index=True)
    winner_id = Column(BigInteger, nullable=True, index=True)
    winner_name = Column(String, nullable=True)
    players = Column(JSONList, default=list)  # [{id, name, place}]
    finished_at = Column(DateTime, default=now_local, index=True)
    # 🆕 To'liq o'yin ma'lumoti (detail modal uchun)
    started_at = Column(DateTime, nullable=True)
    duration_sec = Column(Integer, nullable=True)
    draw_count = Column(Integer, nullable=True)   # jami deckdan olingan kartalar
    turn_count = Column(Integer, nullable=True)   # jami tashlangan kartalar


class Tournament(Base):
    __tablename__ = "tournaments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(8), unique=True, index=True)
    host_id = Column(BigInteger, nullable=False)
    entry_fee = Column(Integer, default=0)
    prize = Column(Integer, default=0)  # jamg'arma (entry x n)
    status = Column(String(16), default="open")  # open | running | finished | cancelled
    players = Column(JSONList, default=list)   # [{id, name}]
    matches = Column(JSONList, default=list)   # bracket: [{round, p1, p2, winner_id}]
    winner_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=now_local)
    finished_at = Column(DateTime, nullable=True)


class ModLogEntry(Base):
    __tablename__ = "mod_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_id = Column(BigInteger, nullable=True, index=True)
    actor_name = Column(String, nullable=True)
    action = Column(String(24), nullable=False, index=True)   # block | unblock | role | delete | cleanup | system
    target_id = Column(BigInteger, nullable=True, index=True)
    text = Column(String, nullable=False)
    created_at = Column(DateTime, default=now_local, index=True)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room = Column(String, default="global", index=True)  # "global" for now
    user_id = Column(BigInteger, nullable=False)
    name = Column(String, nullable=True)
    text = Column(String, nullable=False)
    created_at = Column(DateTime, default=now_local, index=True)


class Friendship(Base):
    __tablename__ = "friendships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)      # who sent the request
    friend_id = Column(BigInteger, nullable=False, index=True)    # who receives it
    # "pending" | "accepted"
    status = Column(String(16), default="pending", nullable=False)
    created_at = Column(DateTime, default=now_local)
