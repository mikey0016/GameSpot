# backend/api/social.py

"""Social features REST API:
- POST /users/me            -> upsert Telegram user + mark online
- POST /users/heartbeat     -> keep online, receive pending invites
- GET  /users/online        -> users seen in the last 60s
- GET  /users/{id}          -> profile with stats
- GET  /users/{id}/history  -> finished games of a user
- GET  /leaderboard         -> top players by wins
- POST /invites             -> send an in-app game invite
- POST /invites/{id}/accept -> accept invite (broadcasts to sender via WS)
- POST /invites/{id}/decline-> decline invite
- POST /chat/global         -> global chat message (relayed over /ws/global)
- POST /games/result        -> record a finished game (history/leaderboard source)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from datetime import datetime, timedelta

from ..models.social import OnlineUser, GameRecord, ChatMessage
from ..database import async_session_maker

router = APIRouter(tags=["social"])

ONLINE_WINDOW = timedelta(seconds=60)


async def get_session():
    yield async_session_maker


# ---------- Schemas ----------

class UserIn(BaseModel):
    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    nickname: str | None = None


class NicknameIn(BaseModel):
    user_id: int
    nickname: str


class InviteIn(BaseModel):
    from_id: int
    from_name: str
    to_id: int
    room_code: str


class ChatIn(BaseModel):
    user_id: int
    name: str
    text: str


class GameResultIn(BaseModel):
    room_code: str
    winner_id: int | None = None
    winner_name: str | None = None
    players: list[dict] = []


# ---------- Helpers ----------

async def _broadcast(message: dict):
    """Broadcast over the shared WebSocket manager (room 'global' pseudo-room)."""
    from ..main import manager
    await manager.broadcast("global", message)


def _user_public(u: OnlineUser) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "nickname": u.nickname,
        "role": u.role,
        "blocked": bool(u.blocked),
        "display_name": u.nickname or u.first_name or u.username,
        "last_online": u.last_online.isoformat() if u.last_online else None,
        "games_played": u.games_played,
        "wins": u.wins,
        "losses": u.losses,
        "xp": u.xp,
        "level": u.level,
    }


def _display_name(u: OnlineUser) -> str:
    return u.nickname or u.first_name or u.username or "O'yinchi"


def _record_public(r: GameRecord) -> dict:
    return {
        "id": r.id,
        "room_code": r.room_code,
        "winner_id": r.winner_id,
        "winner_name": r.winner_name,
        "players": r.players or [],
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


# ---------- Users ----------

@router.post("/users/me")
async def upsert_user(req: UserIn, session=Depends(get_session)):
    """Called when the WebApp opens: create/update user and mark online."""
    async with session() as db:
        u = await db.get(OnlineUser, req.id)
        if not u:
            u = OnlineUser(id=req.id)
            db.add(u)
        u.username = req.username or u.username
        u.first_name = req.first_name or u.first_name
        u.last_name = req.last_name or u.last_name
        u.last_online = datetime.utcnow()
        if req.nickname is not None:
            u.nickname = req.nickname[:24] or None
        u.invites = u.invites or []
        await db.commit()
        await db.refresh(u)
        return _user_public(u)


@router.post("/users/heartbeat")
async def heartbeat(req: UserIn, session=Depends(get_session)):
    """Refresh online status; returns invites still pending for this user."""
    async with session() as db:
        u = await db.get(OnlineUser, req.id)
        if not u:
            u = OnlineUser(id=req.id, username=req.username, first_name=req.first_name)
            db.add(u)
        u.last_online = datetime.utcnow()
        u.invites = u.invites or []
        await db.commit()
        pending = [i for i in u.invites if i.get("status") == "pending"]
        return {"ok": True, "invites": pending, "blocked": bool(u.blocked), "role": u.role}


@router.get("/users/online")
async def online_users(session=Depends(get_session)):
    from sqlalchemy import select
    async with session() as db:
        cutoff = datetime.utcnow() - ONLINE_WINDOW
        result = await db.execute(
            select(OnlineUser)
            .where(OnlineUser.last_online >= cutoff)
            .order_by(OnlineUser.last_online.desc())
            .limit(100)
        )
        users = result.scalars().all()
        return {"users": [_user_public(u) for u in users]}


@router.get("/users/{user_id}")
async def get_profile(user_id: int, session=Depends(get_session)):
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        if not u:
            # Non-online users may still have played before invites existed
            hist = (
                await db.execute(
                    select(GameRecord).where(GameRecord.winner_id == user_id)
                )
            ).scalars().all()
            return {
                "id": user_id,
                "username": None,
                "first_name": None,
                "nickname": None,
                "games_played": len(hist),
                "wins": len(hist),
                "losses": 0,
                "xp": len(hist) * 10,
                "level": 1 + len(hist) // 5,
            }
        return {
            "id": u.id,
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "nickname": u.nickname,
            "games_played": u.games_played,
            "wins": u.wins,
            "losses": u.losses,
            "xp": u.xp,
            "level": u.level,
        }


@router.patch("/users/me/nickname")
async def set_nickname(req: NicknameIn, session=Depends(get_session)):
    """Change the in-app display nickname (1-24 chars)."""
    nick = (req.nickname or "").strip()[:24]
    if not nick:
        raise HTTPException(400, "Nickname bo'sh bo'lmasligi kerak")
    async with session() as db:
        u = await db.get(OnlineUser, req.user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        u.nickname = nick
        await db.commit()
        return {"ok": True, "nickname": u.nickname, "display_name": _display_name(u)}


@router.get("/users/{user_id}/history")
async def user_history(user_id: int, session=Depends(get_session)):
    async with session() as db:
        # Load recent records and filter in Python (JSON contains is backend-specific)
        all_rows = (
            await db.execute(
                select(GameRecord)
                .order_by(GameRecord.finished_at.desc())
                .limit(200)
            )
        ).scalars().all()
        rows = [
            r for r in all_rows
            if any(p.get("id") == user_id for p in (r.players or []))
        ][:50]
        return {"history": [_record_public(r) for r in rows]}


# ---------- Leaderboard ----------

@router.get("/leaderboard")
async def leaderboard(session=Depends(get_session)):
    async with session() as db:
        rows = (
            await db.execute(
                select(OnlineUser)
                .order_by(OnlineUser.wins.desc(), OnlineUser.xp.desc())
                .limit(20)
            )
        ).scalars().all()
        return {
            "leaderboard": [
                {
                    "rank": i + 1,
                    "id": u.id,
                    "name": _display_name(u),
                    "wins": u.wins,
                    "games": u.games_played,
                    "level": u.level,
                }
                for i, u in enumerate(rows)
                if u.wins > 0 or u.games_played > 0
            ]
        }


# ---------- Invites ----------

@router.post("/invites")
async def send_invite(req: InviteIn, session=Depends(get_session)):
    async with session() as db:
        if await _is_blocked(db, req.from_id):
            raise HTTPException(403, "Siz bloklangansiz")
        target = await db.get(OnlineUser, req.to_id)
        if not target:
            raise HTTPException(404, "Foydalanuvchi onlayn emas")
        invite = {
            "id": f"{req.to_id}-{int(datetime.utcnow().timestamp() * 1000)}",
            "from_id": req.from_id,
            "from_name": req.from_name,
            "room_code": req.room_code,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
        }
        target.invites = (target.invites or []) + [invite]
        await db.commit()
        await _broadcast({"type": "invite", "invite": invite})
        return {"ok": True, "invite_id": invite["id"]}


async def _resolve_invite(db, invite_id: int | str, status: str):
    """Find an invite by id across users, set its status, return (invite, owner)."""
    users = (await db.execute(select(OnlineUser))).scalars().all()
    for u in users:
        for inv in u.invites or []:
            if str(inv.get("id")) == str(invite_id):
                inv["status"] = status
                u.invites = u.invites or []
                u.invites = list(u.invites)
                return inv, u
    return None, None


@router.post("/invites/{invite_id}/accept")
async def accept_invite(invite_id: str, session=Depends(get_session)):
    async with session() as db:
        inv, _ = await _resolve_invite(db, invite_id, "accepted")
        if not inv:
            raise HTTPException(404, "Taklif topilmadi")
        await db.commit()
        await _broadcast({
            "type": "invite_accepted",
            "invite_id": str(invite_id),
            "room_code": inv["room_code"],
            "from_id": inv["from_id"],
        })
        return {"ok": True, "room_code": inv["room_code"]}


@router.post("/invites/{invite_id}/decline")
async def decline_invite(invite_id: str, session=Depends(get_session)):
    async with session() as db:
        inv, _ = await _resolve_invite(db, invite_id, "declined")
        if not inv:
            raise HTTPException(404, "Taklif topilmadi")
        await db.commit()
        await _broadcast({
            "type": "invite_declined",
            "invite_id": str(invite_id),
            "from_id": inv["from_id"],
        })
        return {"ok": True}


# ---------- Global chat ----------

@router.post("/chat/global")
async def global_chat(req: ChatIn, session=Depends(get_session)):
    if not req.text.strip() or len(req.text) > 300:
        raise HTTPException(400, "Xato matn")
    async with session() as db:
        sender = await db.get(OnlineUser, req.user_id)
        if sender and sender.blocked:
            raise HTTPException(403, "Siz bloklangansiz")
        msg = ChatMessage(
            user_id=req.user_id,
            name=req.name or "O'yinchi",
            text=req.text.strip(),
        )
        db.add(msg)
        await db.commit()
    payload = {
        "type": "chat",
        "room": "global",
        "user_id": req.user_id,
        "name": req.name or "O'yinchi",
        "text": req.text.strip(),
        "ts": datetime.utcnow().isoformat(),
    }
    await _broadcast(payload)
    return {"ok": True}


@router.get("/chat/global")
async def global_chat_history(session=Depends(get_session)):
    async with session() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.room == "global")
                .order_by(ChatMessage.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        return {
            "messages": [
                {
                    "user_id": m.user_id,
                    "name": m.name,
                    "text": m.text,
                    "ts": m.created_at.isoformat() if m.created_at else None,
                }
                for m in reversed(rows)
            ]
        }


# ---------- Game results ----------

@router.post("/games/result")
async def record_game_result(req: GameResultIn, session=Depends(get_session)):
    """Called by the client when a game finishes; updates stats + history."""
    async with session() as db:
        rec = GameRecord(
            room_code=req.room_code,
            winner_id=req.winner_id,
            winner_name=req.winner_name,
            players=req.players,
        )
        db.add(rec)

        for p in req.players:
            pid = p.get("id")
            if pid is None:
                continue
            u = await db.get(OnlineUser, pid)
            if not u:
                u = OnlineUser(id=pid, first_name=p.get("name"))
                db.add(u)
            # Refresh names on each recorded game so nickname changes propagate
            elif p.get("name"):
                u.first_name = u.first_name or p.get("name")
            u.games_played = (u.games_played or 0) + 1
            if pid == req.winner_id:
                u.wins = (u.wins or 0) + 1
                u.xp = (u.xp or 0) + 20
            else:
                u.losses = (u.losses or 0) + 1
                u.xp = (u.xp or 0) + 5
            u.level = 1 + (u.xp or 0) // 50

        await db.commit()
        return {"ok": True}


# ---------- Owner panel (only role='owner') ----------

async def _require_owner(user_id: int, db) -> OnlineUser:
    u = await db.get(OnlineUser, user_id)
    if not u or u.role != "owner":
        raise HTTPException(403, "Faqat owner uchun")
    return u


async def _is_blocked(db, user_id: int) -> bool:
    u = await db.get(OnlineUser, user_id)
    return bool(u and u.blocked)


async def _broadcast_stats_update(db, user_id: int):
    """Push fresh user info to all WebApp clients via global WebSocket.
    Targets update their profile/tags live, without a reload."""
    u = await db.get(OnlineUser, user_id)
    if not u:
        return
    await _broadcast({
        "type": "stats_update",
        "user_id": user_id,
        "user": _user_public(u),
    })


@router.get("/owner/check/{user_id}")
async def owner_check(user_id: int, session=Depends(get_session)):
    """Frontend asks this to decide which panel button to show (owner/admin)."""
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        return {
            "is_owner": bool(u and u.role == "owner"),
            "is_admin": bool(u and u.role in ("owner", "admin")),
            "role": (u.role if u else None),
        }


@router.get("/owner/stats")
async def owner_stats(owner_id: int, session=Depends(get_session)):
    async with session() as db:
        await _require_owner(owner_id, db)
        users = (await db.execute(select(func.count()).select_from(OnlineUser))).scalar() or 0
        online = (await db.execute(
            select(func.count()).select_from(OnlineUser)
            .where(OnlineUser.last_online >= datetime.utcnow() - ONLINE_WINDOW)
        )).scalar() or 0
        from ..models.room import Room, RoomStatus
        rooms_total = (await db.execute(select(func.count()).select_from(Room))).scalar() or 0
        rooms_open = (await db.execute(
            select(func.count()).select_from(Room).where(Room.status == RoomStatus.WAITING)
        )).scalar() or 0
        games = (await db.execute(select(func.count()).select_from(GameRecord))).scalar() or 0
        msgs = (await db.execute(select(func.count()).select_from(ChatMessage))).scalar() or 0
        return {
            "users": users,
            "online": online,
            "rooms_total": rooms_total,
            "rooms_open": rooms_open,
            "games": games,
            "messages": msgs,
        }


@router.get("/owner/users")
async def owner_users(owner_id: int, session=Depends(get_session)):
    async with session() as db:
        await _require_owner(owner_id, db)
        rows = (
            await db.execute(
                select(OnlineUser).order_by(OnlineUser.last_online.desc()).limit(200)
            )
        ).scalars().all()
        return {
            "users": [
                {
                    "id": u.id,
                    "name": _display_name(u),
                    "username": u.username,
                    "role": u.role,
                    "blocked": bool(u.blocked),
                    "games": u.games_played,
                    "wins": u.wins,
                    "xp": u.xp,
                    "level": u.level,
                    "losses": u.losses,
                    "last_online": u.last_online.isoformat() if u.last_online else None,
                }
                for u in rows
            ]
        }


class OwnerDeleteIn(BaseModel):
    owner_id: int
    user_id: int


@router.delete("/owner/users/{user_id}")
async def owner_delete_user(user_id: int, owner_id: int, session=Depends(get_session)):
    """Delete a user (and their stats); owner cannot delete themselves."""
    async with session() as db:
        await _require_owner(owner_id, db)
        if user_id == owner_id:
            raise HTTPException(400, "O'zingizni o'chira olmaysiz")
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        # remove their game history entries (players is JSON with ids)
        records = (await db.execute(select(GameRecord))).scalars().all()
        for r in records:
            if any(p.get("id") == user_id for p in (r.players or [])):
                await db.delete(r)
        await db.delete(u)
        await db.commit()
        return {"ok": True}


class BroadcastIn(BaseModel):
    owner_id: int
    text: str


@router.post("/owner/broadcast")
async def owner_broadcast(req: BroadcastIn, session=Depends(get_session)):
    """Send a system message to every connected WebApp client (global chat + toast)."""
    async with session() as db:
        await _require_owner(req.owner_id, db)
    text = (req.text or "").strip()[:300]
    if not text:
        raise HTTPException(400, "Bo'sh xabar")
    msg = ChatMessage(user_id=0, name="📢 SYSTEM", text=text)
    async with session() as s:
        s.add(msg)
        await s.commit()
    await _broadcast({
        "type": "chat",
        "room": "global",
        "user_id": 0,
        "name": "📢 SYSTEM",
        "text": text,
        "ts": datetime.utcnow().isoformat(),
    })
    return {"ok": True}


# ---------- Owner user management actions ----------

class BlockIn(BaseModel):
    owner_id: int
    blocked: bool


@router.post("/owner/users/{user_id}/block")
async def owner_block_user(user_id: int, req: BlockIn, session=Depends(get_session)):
    """Block/unblock a user: chat, invites and room joins are refused."""
    async with session() as db:
        await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if u.role == "owner":
            raise HTTPException(400, "Ownerni bloklash mumkin emas")
        u.blocked = 1 if req.blocked else 0
        await db.commit()
        await _broadcast({
            "type": "user_blocked",
            "user_id": user_id,
            "blocked": bool(u.blocked),
        })
        await _broadcast_stats_update(db, user_id)
        return {"ok": True, "blocked": bool(u.blocked)}


class XpIn(BaseModel):
    owner_id: int
    amount: int


@router.post("/owner/users/{user_id}/xp")
async def owner_add_xp(user_id: int, req: XpIn, session=Depends(get_session)):
    """Add (or subtract with negative amount) XP; level is recalculated."""
    async with session() as db:
        await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        u.xp = max(0, (u.xp or 0) + req.amount)
        u.level = 1 + (u.xp or 0) // 50
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        return {"ok": True, "xp": u.xp, "level": u.level}


class LevelIn(BaseModel):
    owner_id: int
    level: int


@router.post("/owner/users/{user_id}/level")
async def owner_set_level(user_id: int, req: LevelIn, session=Depends(get_session)):
    """Set level directly; XP is aligned so future games keep the level."""
    level = max(1, req.level)
    async with session() as db:
        await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        u.level = level
        u.xp = (level - 1) * 50
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        return {"ok": True, "xp": u.xp, "level": u.level}


class RoleIn(BaseModel):
    owner_id: int
    role: str  # "owner" | "admin" | "player"


@router.post("/owner/users/{user_id}/role")
async def owner_set_role(user_id: int, req: RoleIn, session=Depends(get_session)):
    """Assign a tag: owner / admin / player."""
    role = (req.role or "player").lower()
    if role not in ("owner", "admin", "player"):
        raise HTTPException(400, "Role: owner | admin | player")
    async with session() as db:
        await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if user_id == req.owner_id:
            raise HTTPException(400, "O'z rolingizni o'zgartira olmaysiz")
        if u.role == "owner" and role != "owner":
            raise HTTPException(400, "Ownerni pasaytirish mumkin emas")
        u.role = None if role == "player" else role
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        return {"ok": True, "role": u.role or "player"}


class StatsIn(BaseModel):
    owner_id: int
    games: int | None = None
    wins: int | None = None
    losses: int | None = None
    xp: int | None = None


@router.post("/owner/users/{user_id}/stats")
async def owner_edit_stats(user_id: int, req: StatsIn, session=Depends(get_session)):
    """Overwrite statistics values; level is recalculated from XP."""
    async with session() as db:
        await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if req.games is not None:
            u.games_played = max(0, req.games)
        if req.wins is not None:
            u.wins = max(0, req.wins)
        if req.losses is not None:
            u.losses = max(0, req.losses)
        if req.xp is not None:
            u.xp = max(0, req.xp)
            u.level = 1 + (u.xp or 0) // 50
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        return {"ok": True, "user": _user_public(u)}


# ---------- Admin panel (owner + admin) ----------

async def _require_admin(user_id: int, db) -> OnlineUser:
    u = await db.get(OnlineUser, user_id)
    if not u or u.role not in ("owner", "admin"):
        raise HTTPException(403, "Faqat owner/admin uchun")
    return u


@router.get("/admin/stats")
async def admin_stats(admin_id: int, session=Depends(get_session)):
    """Limited counters for the admin panel."""
    async with session() as db:
        await _require_admin(admin_id, db)
        users = (await db.execute(select(func.count()).select_from(OnlineUser))).scalar() or 0
        online = (await db.execute(
            select(func.count()).select_from(OnlineUser)
            .where(OnlineUser.last_online >= datetime.utcnow() - ONLINE_WINDOW)
        )).scalar() or 0
        games = (await db.execute(select(func.count()).select_from(GameRecord))).scalar() or 0
        return {"users": users, "online": online, "games": games}


@router.get("/admin/users")
async def admin_users(admin_id: int, session=Depends(get_session)):
    async with session() as db:
        await _require_admin(admin_id, db)
        rows = (
            await db.execute(
                select(OnlineUser).order_by(OnlineUser.last_online.desc()).limit(200)
            )
        ).scalars().all()
        return {
            "users": [
                {
                    "id": u.id,
                    "name": _display_name(u),
                    "username": u.username,
                    "role": u.role,
                    "blocked": bool(u.blocked),
                    "games": u.games_played,
                    "wins": u.wins,
                    "losses": u.losses,
                    "xp": u.xp,
                    "level": u.level,
                    "last_online": u.last_online.isoformat() if u.last_online else None,
                }
                for u in rows
            ]
        }


class AdminBlockIn(BaseModel):
    admin_id: int
    blocked: bool


@router.post("/admin/users/{user_id}/block")
async def admin_block_user(user_id: int, req: AdminBlockIn, session=Depends(get_session)):
    """Admins may block/unblock regular players only (not owners/admins/self)."""
    async with session() as db:
        await _require_admin(req.admin_id, db)
        if user_id == req.admin_id:
            raise HTTPException(400, "O'zingizni bloklash mumkin emas")
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if u.role == "owner":
            raise HTTPException(400, "Ownerni bloklash mumkin emas")
        if u.role == "admin":
            raise HTTPException(400, "Adminni bloklash mumkin emas")
        u.blocked = 1 if req.blocked else 0
        await db.commit()
        await _broadcast({
            "type": "user_blocked",
            "user_id": user_id,
            "blocked": bool(u.blocked),
        })
        await _broadcast_stats_update(db, user_id)
        return {"ok": True, "blocked": bool(u.blocked)}
