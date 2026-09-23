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
from sqlalchemy import select, func, delete
from datetime import datetime, timedelta

from ..models.social import OnlineUser, GameRecord, ChatMessage, Friendship, ModLogEntry, Tournament
from ..database import async_session_maker
from ..tz import now_local
from ..config import settings

router = APIRouter(tags=["social"])

ONLINE_WINDOW = timedelta(seconds=60)

# ----- Role hierarchy: lower number = more power -----
# 💎 MO (-1) > 💠 Co-Owner (0) > 👑 Owner (1) > 🛡 Moderator (2) > ⭐ Deputy (3) > 🎖 Admin (4) > 👤 Player (5)
ROLE_RANK = {"main_owner": -1, "co_owner": 0, "owner": 1, "moderator": 2, "deputy": 3, "admin": 4, None: 5}
VALID_ROLES = ("main_owner", "co_owner", "owner", "moderator", "deputy", "admin", "player")
# Panellarga kirish darajasi: MO, Co-Owner, Owner — owner panel; moderator — admin panel
OWNER_LEVEL = ("main_owner", "co_owner", "owner")
CO_LEVEL = ("main_owner", "co_owner")


def _can_manage(actor, target) -> bool:
    """Actor may manage target only if strictly higher in the hierarchy.
    main_owner may manage EVERYONE except themselves (including other main_owners)."""
    if not actor or not target:
        return False
    if actor.id == target.id:
        return False
    if actor.role == "main_owner":
        return True
    return ROLE_RANK.get(actor.role, 5) < ROLE_RANK.get(target.role, 5)


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


def _market_public(eq: dict) -> dict:
    """Kiygan kosmetikani frontend uchun url'lar bilan qaytaradi (badge/avatar/frame/banner/bg)."""
    return {
        "cosmetics": {
            kind: (item["url"] if item else None)
            for kind, item in eq.items()
        }
    }


def _cosmetics_of(u: OnlineUser) -> dict:
    """OnlineUser'dan to'g'ridan-to'g'ri cosmetics dict (lazy import yo'q — tez)."""
    from .fun import _equipped_market
    return _market_public(_equipped_market(u))["cosmetics"]


def _user_public(u: OnlineUser) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "nickname": u.nickname,
        "role": u.role,
        "blocked": bool(u.blocked),
        "blocked_until": u.blocked_until.isoformat() if u.blocked_until else None,
        "muted": bool(u.muted),
        "muted_until": u.muted_until.isoformat() if u.muted_until else None,
        "warning": u.warning,
        "display_name": u.nickname or u.first_name or u.username,
        "last_online": u.last_online.isoformat() if u.last_online else None,
        "games_played": u.games_played,
        "wins": u.wins,
        "losses": u.losses,
        "xp": u.xp,
        "level": u.level,
        "coins": u.coins or 0,
        "likes": u.likes or 0,
        "cosmetics": _cosmetics_of(u),
    }


def _display_name(u: OnlineUser) -> str:
    return u.nickname or u.first_name or u.username or "O'yinchi"


def _owner_action(actor: OnlineUser, target: OnlineUser) -> bool:
    """Permission gate for panel actions on a target user (pure — side-effect yo'q).
    💎 main_owner: hamma ustida (o'zidan boshqa) — hech qanday cheklov yo'q
    💠 co_owner: pastdagi barcha rollar (owner, moderator, deputy, admin, player)
    👑 owner: moderator/deputy/admin/player
    🛡 moderator: deputy/admin/player
    ⭐ deputy: admin/player
    🎖 admin: player"""
    if target.id == actor.id:
        return False
    if actor.role == "main_owner":
        return True
    return ROLE_RANK.get(actor.role, 5) < ROLE_RANK.get(target.role, 5)


async def _modlog(db, actor: OnlineUser | None, action: str, text: str,
                  target_id: int | None = None):
    """Persist a moderation event and push it live to open panel Log tabs."""
    db.add(ModLogEntry(
        actor_id=actor.id if actor else None,
        actor_name=_display_name(actor) if actor else "SYSTEM",
        action=action,
        target_id=target_id,
        text=text[:220],
    ))
    await db.commit()
    try:
        await _broadcast({"type": "modlog_new"})
    except Exception:
        pass


async def _cleanup_rooms(db) -> int:
    """Delete stale lobbies and orphaned finished rooms. Returns removed count."""
    from ..models.room import Room, RoomStatus
    from ..main import games as active_games
    now = now_local()
    removed = 0
    rows = (await db.execute(select(Room))).scalars().all()
    for r in rows:
        status = r.status.value if hasattr(r.status, "value") else str(r.status)
        age_min = (now - r.created_at).total_seconds() / 60.0 if r.created_at else 999
        stale = (status == "WAITING" and age_min > 30)
        orphan = (status == "FINISHED" and code_empty(r.id))
        if stale or orphan:
            await db.delete(r)
            removed += 1
    if removed:
        await db.commit()
    return removed


def code_empty(code: str) -> bool:
    """True when nobody is connected to the room socket anymore."""
    try:
        from ..main import manager
        return not manager.active_connections.get(code)
    except Exception:
        return True


def _record_public(r: GameRecord) -> dict:
    return {
        "id": r.id,
        "room_code": r.room_code,
        "winner_id": r.winner_id,
        "winner_name": r.winner_name,
        "players": r.players or [],
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "duration_sec": r.duration_sec,
        "draw_count": r.draw_count,
        "turn_count": r.turn_count,
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
        u.last_online = now_local()
        if req.nickname is not None:
            u.nickname = req.nickname[:24] or None
        u.invites = u.invites or []
        # ADMIN_ID (env) doim main_owner: rol boshqa tomonidan pasaytirilgan bo'lsa ham qayta ko'tariladi
        if req.id == settings.ADMIN_ID and u.role != "main_owner":
            u.role = "main_owner"
        await db.commit()
        await db.refresh(u)
        return _user_public(u)


@router.post("/users/heartbeat")
async def heartbeat(req: UserIn, session=Depends(get_session)):
    """Refresh online status; returns invites, block state (auto-expires timed blocks)."""
    async with session() as db:
        await _expire_blocks(db)
        u = await db.get(OnlineUser, req.id)
        if not u:
            u = OnlineUser(id=req.id, username=req.username, first_name=req.first_name)
            db.add(u)
        u.last_online = now_local()
        u.invites = u.invites or []
        # ADMIN_ID doim main_owner bo'lib qolishi kerak (heartbeat orqali ham)
        if req.id == settings.ADMIN_ID and u.role != "main_owner":
            u.role = "main_owner"
            await db.commit()
            await db.refresh(u)
        # Eski pending invite'larni tozalash (24 soatdan oshganlar yashirinadi + o'chiriladi)
        cutoff = now_local()
        from datetime import datetime as _dt, timedelta as _td
        fresh = []
        expired_ids = []
        for i in u.invites:
            if i.get("status") != "pending":
                continue
            try:
                created = _dt.fromisoformat(i.get("created_at"))
            except Exception:
                created = None
            if created and (cutoff - created) > _td(hours=24):
                i["status"] = "expired"
                expired_ids.append(str(i.get("id")))
            else:
                fresh.append(i)
        if expired_ids:
            u.invites = [i for i in (u.invites or []) if str(i.get("id")) not in expired_ids] or []
            await db.commit()
        pending = [i for i in fresh]
        # ❗ Invite spam: faqat oxirgi 3 ta pending qaytariladi
        pending = pending[-3:]
        return {
            "ok": True,
            "invites": pending,
            "blocked": bool(u.blocked),
            "blocked_until": u.blocked_until.isoformat() if u.blocked_until else None,
            "block_reason": u.block_reason,
            "muted": bool(u.muted),
            "muted_until": u.muted_until.isoformat() if u.muted_until else None,
            "warning": u.warning,
            "role": u.role,
        }


@router.get("/users/subscribe-status/{user_id}")
async def subscribe_status(user_id: int, session=Depends(get_session)):
    """WebApp uchun kanal obuna holati (Telegram Bot API getChatMember)."""
    import asyncio
    import urllib.request
    import json as _json
    from ..config import settings

    subscribed = True  # fail-open: xato bo'lsa qulf CIMAYMIZ
    channel = settings.CHANNEL_URL
    token = settings.BOT_TOKEN
    if channel and token:
        uname = channel.rstrip("/").split("/")[-1]
        if not uname.startswith("@"):
            uname = "@" + uname
        url = f"https://api.telegram.org/bot{token}/getChatMember?chat_id={uname}&user_id={user_id}"

        def _fetch():
            with urllib.request.urlopen(url, timeout=4) as r:
                return _json.loads(r.read().decode())

        try:
            data = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=6.0)
            if data.get("ok"):
                status = (data.get("result") or {}).get("status", "")
                subscribed = status in ("member", "administrator", "creator")
        except Exception:
            subscribed = True  # bot kanalda admin emas / API xatosi -> bloklamaymiz
    return {"subscribed": subscribed, "channel": channel}


@router.get("/users/online")
async def online_users(session=Depends(get_session)):
    from sqlalchemy import select
    async with session() as db:
        cutoff = now_local() - ONLINE_WINDOW
        result = await db.execute(
            select(OnlineUser)
            .where(OnlineUser.last_online >= cutoff)
            .order_by(OnlineUser.last_online.desc())
            .limit(100)
        )
        users = result.scalars().all()
        return {"users": [_user_public(u) for u in users]}


@router.get("/users/search")
async def search_users(q: str, session=Depends(get_session)):
    """Search by exact username / nickname / first_name / numeric ID (all users, not only online)."""
    q = (q or "").strip().lstrip("@")
    if not q:
        raise HTTPException(400, "Qidiruv bo'sh")
    async with session() as db:
        u = None
        if q.isdigit():
            u = await db.get(OnlineUser, int(q))
        if not u:
            ql = q.lower()
            rows = (await db.execute(
                select(OnlineUser).where(
                    (func.lower(OnlineUser.username) == ql) |
                    (func.lower(OnlineUser.nickname) == ql) |
                    (func.lower(OnlineUser.first_name) == ql)
                ).limit(5)
            )).scalars().all()
            u = rows[0] if rows else None
            matches = [{"id": r.id, "name": _display_name(r), "username": r.username} for r in rows]
        else:
            matches = [{"id": u.id, "name": _display_name(u), "username": u.username}]
        if not u:
            raise HTTPException(404, "User topilmadi")
        return {"id": u.id, "name": _display_name(u), "username": u.username, "matches": matches}


@router.get("/users/{user_id}")
async def get_profile(user_id: int, session=Depends(get_session)):
    from .fun import _equipped_market
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
                "role": None,
                "blocked": False,
                "blocked_until": None,
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
            "role": u.role,
            "blocked": bool(u.blocked),
            "blocked_until": u.blocked_until.isoformat() if u.blocked_until else None,
            "games_played": u.games_played,
            "wins": u.wins, "losses": u.losses,
            "xp": u.xp,
            "level": u.level,
            "coins": u.coins or 0,
            "likes": u.likes or 0,
            **_market_public(_equipped_market(u)),
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
    from .fun import _equipped_market
    async with session() as db:
        rows = (
            await db.execute(
                select(OnlineUser)
                .order_by(OnlineUser.wins.desc(), OnlineUser.xp.desc())
                .limit(20)
            )
        ).scalars().all()
        now = now_local()
        return {
            "leaderboard": [
                {
                    "rank": i + 1,
                    "id": u.id,
                    "name": _display_name(u),
                    "username": u.username,
                    "role": u.role,
                    "online": bool(u.last_online and u.last_online >= now - ONLINE_WINDOW),
                    "blocked": bool(u.blocked),
                    "wins": u.wins,
                    "games": u.games_played,
                    "level": u.level,
                    "likes": u.likes or 0,
                    "coins": u.coins or 0,
                    **_market_public(_equipped_market(u)),
                }
                for i, u in enumerate(rows)
                if u.wins > 0 or u.games_played > 0
            ]
        }


@router.get("/users/{user_id}/mini")
async def user_mini_profile(user_id: int, viewer_id: int = 0, session=Depends(get_session)):
    """Leaderboard card popup: short stats + friend-request state (viewer_id orqali)."""
    from .fun import _equipped_market
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        now = now_local()
        online = bool(u.last_online and u.last_online >= now - ONLINE_WINDOW)
        # get_profile'dagi fallback: hech qachon online bo'lmagan lekin o'ynagan userlar
        games = u.games_played or 0
        wins = u.wins or 0
        if not u.last_online and not games:
            hist = (await db.execute(
                select(GameRecord).where(GameRecord.winner_id == user_id)
            )).scalars().all()
            games = len(hist)
            wins = len(hist)
        friend_state = None
        friend_req_id = None
        friend_incoming = False
        if viewer_id and viewer_id != user_id:
            f = (await db.execute(
                select(Friendship).where(
                    ((Friendship.user_id == viewer_id) & (Friendship.friend_id == user_id)) |
                    ((Friendship.user_id == user_id) & (Friendship.friend_id == viewer_id))
                )
            )).scalars().first()
            if f:
                friend_state = f.status
                friend_req_id = f.id
                friend_incoming = (f.status == "pending" and f.friend_id == viewer_id)
        return {
            "id": u.id,
            "name": _display_name(u),
            "username": u.username,
            "role": u.role or "player",
            "online": online,
            "blocked": bool(u.blocked),
            "games": games,
            "wins": wins,
            "losses": u.losses or 0,
            "level": u.level or 1,
            "xp": u.xp or 0,
            "coins": u.coins or 0,
            "friend_state": friend_state,  # None | pending | accepted
            "friend_req_id": friend_req_id,
            "friend_incoming": friend_incoming,
            "likes": u.likes or 0,
            "liked_by_me": bool(viewer_id and str(viewer_id) in [str(x) for x in (u.liked_by or [])]),
            **_market_public(_equipped_market(u)),
        }


# ---------- Friends ----------

def _friend_public(u: OnlineUser | None) -> dict:
    if not u:
        return {}
    online = bool(u.last_online and u.last_online >= now_local() - ONLINE_WINDOW)
    return {
        "id": u.id,
        "name": _display_name(u),
        "username": u.username,
        "role": u.role,
        "blocked": bool(u.blocked),
        "online": online,
        "level": u.level,
        "wins": u.wins,
        "cosmetics": _cosmetics_of(u) if u else {"badge": None, "frame": None, "skin": None, "banner": None, "bg": None},
    }


class FriendReqIn(BaseModel):
    from_id: int
    to_id: int


class FriendActionIn(BaseModel):
    user_id: int
    friend_id: int


@router.get("/friends/{user_id}")
async def list_friends(user_id: int, session=Depends(get_session)):
    """Accepted friends + incoming pending requests."""
    async with session() as db:
        rows = (await db.execute(
            select(Friendship).where(
                (Friendship.user_id == user_id) | (Friendship.friend_id == user_id)
            )
        )).scalars().all()
        friends, incoming = [], []
        for r in rows:
            other_id = r.friend_id if r.user_id == user_id else r.user_id
            other = await db.get(OnlineUser, other_id)
            if r.status == "accepted":
                friends.append(_friend_public(other))
            elif r.status == "pending" and r.friend_id == user_id:
                incoming.append({**_friend_public(other), "request_id": r.id})
        return {"friends": friends, "incoming": incoming}


@router.post("/friends")
async def add_friend(req: FriendReqIn, session=Depends(get_session)):
    """Send a friend request (target must exist; no duplicates)."""
    if req.from_id == req.to_id:
        raise HTTPException(400, "O'zingizni do'st qo'sha olmaysiz")
    async with session() as db:
        target = await db.get(OnlineUser, req.to_id)
        if not target:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        existing = (await db.execute(
            select(Friendship).where(
                ((Friendship.user_id == req.from_id) & (Friendship.friend_id == req.to_id)) |
                ((Friendship.user_id == req.to_id) & (Friendship.friend_id == req.from_id))
            )
        )).scalars().first()
        if existing:
            if existing.status == "accepted":
                return {"ok": True, "already": True}
            # They already asked us -> auto-accept
            if existing.user_id == req.to_id:
                existing.status = "accepted"
                await db.commit()
                return {"ok": True, "auto_accepted": True}
            return {"ok": True, "pending": True}
        fr = Friendship(user_id=req.from_id, friend_id=req.to_id, status="pending")
        db.add(fr)
        await db.commit()
        await _broadcast({
            "type": "friend_request",
            "from_id": req.from_id,
            "from_name": (await db.get(OnlineUser, req.from_id)).first_name or "O'yinchi",
            "to_id": req.to_id,
        })
        return {"ok": True}


@router.post("/friends/accept")
async def accept_friend(req: FriendActionIn, session=Depends(get_session)):
    async with session() as db:
        fr = (await db.execute(
            select(Friendship).where(
                Friendship.user_id == req.friend_id,
                Friendship.friend_id == req.user_id,
                Friendship.status == "pending",
            )
        )).scalars().first()
        if not fr:
            raise HTTPException(404, "So'rov topilmadi")
        fr.status = "accepted"
        await db.commit()
        await _broadcast({"type": "friend_accepted", "user_id": req.user_id, "friend_id": req.friend_id})
        return {"ok": True}


@router.post("/friends/decline")
async def decline_friend(req: FriendActionIn, session=Depends(get_session)):
    """Reject an incoming pending request (user = receiver, friend = sender)."""
    async with session() as db:
        fr = (await db.execute(
            select(Friendship).where(
                Friendship.user_id == req.friend_id,
                Friendship.friend_id == req.user_id,
                Friendship.status == "pending",
            )
        )).scalars().first()
        if not fr:
            raise HTTPException(404, "So'rov topilmadi")
        await db.delete(fr)
        await db.commit()
        return {"ok": True}


@router.post("/friends/remove")
async def remove_friend(req: FriendActionIn, session=Depends(get_session)):
    async with session() as db:
        fr = (await db.execute(
            select(Friendship).where(
                ((Friendship.user_id == req.user_id) & (Friendship.friend_id == req.friend_id)) |
                ((Friendship.user_id == req.friend_id) & (Friendship.friend_id == req.user_id))
            )
        )).scalars().first()
        if not fr:
            raise HTTPException(404, "Do'stlik topilmadi")
        await db.delete(fr)
        await db.commit()
        return {"ok": True}


# ---------- Invites ----------

@router.post("/invites")
async def send_invite(req: InviteIn, session=Depends(get_session)):
    async with session() as db:
        if await _is_blocked(db, req.from_id):
            raise HTTPException(403, "Siz bloklangansiz")
        target = await db.get(OnlineUser, req.to_id)
        if not target:
            raise HTTPException(404, "Foydalanuvchi onlayn emas")
        # Invite faqat do'stlarga: both users must have an accepted friendship
        friendship = (await db.execute(
            select(Friendship).where(
                ((Friendship.user_id == req.from_id) & (Friendship.friend_id == req.to_id)) |
                ((Friendship.user_id == req.to_id) & (Friendship.friend_id == req.from_id)),
                Friendship.status == "accepted",
            )
        )).scalars().first()
        if not friendship:
            raise HTTPException(403, "Faqat do'stlaringizga taklif yubora olasiz")
        invite = {
            "id": f"{req.to_id}-{int(now_local().timestamp() * 1000)}",
            "from_id": req.from_id,
            "from_name": req.from_name,
            "room_code": req.room_code,
            "status": "pending",
            "created_at": now_local().isoformat(),
        }
        target.invites = (target.invites or []) + [invite]
        await db.commit()
        await _broadcast({"type": "invite", "invite": invite})
        return {"ok": True, "invite_id": invite["id"]}


@router.post("/invites/like")
async def like_user(req: UserIn, target_id: int = 0, session=Depends(get_session)):
    pass  # placeholder (real endpoint pastda UserIn bilan)


@router.post("/likes/{target_id}")
async def like_target(target_id: int, req: UserIn, session=Depends(get_session)):
    """❤️ Userga like bosish (o'ziga yo'q). Qayta bosish = like olib tashlash."""
    async with session() as db:
        me = await db.get(OnlineUser, req.id)
        if not me:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if await _is_blocked(db, req.id):
            raise HTTPException(403, "Siz bloklangansiz")
        target = await db.get(OnlineUser, target_id)
        if not target:
            raise HTTPException(404, "User topilmadi")
        if target_id == req.id:
            raise HTTPException(400, "O'zingizga like bosolmaysiz")
        liked = [str(x) for x in (target.liked_by or [])]
        mine = str(req.id)
        if mine in liked:
            liked.remove(mine)
            target.likes = max(0, (target.likes or 0) - 1)
            liked_now = False
        else:
            liked.append(mine)
            target.likes = (target.likes or 0) + 1
            liked_now = True
        target.liked_by = liked
        await db.commit()
        try:
            await _broadcast_stats_update(db, target_id)
        except Exception:
            pass
        return {"ok": True, "likes": target.likes, "liked": liked_now}


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
    # fetch cosmetics + role for display everywhere
    cosmetics = None
    role = None
    async with session() as _db2:
        _u = await _db2.get(OnlineUser, req.user_id)
        if _u:
            role = _u.role
            cosmetics = _cosmetics_of(_u)
    payload = {
        "type": "chat",
        "room": "global",
        "user_id": req.user_id,
        "name": req.name or "O'yinchi",
        "role": role,
        "cosmetics": cosmetics,
        "text": req.text.strip(),
        "ts": now_local().isoformat(),
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
        # enrich with role/cosmetics so history also shows cosmetics everywhere
        cache = {}
        async def _meta(uid):
            if uid in cache:
                return cache[uid]
            u = await db.get(OnlineUser, uid)
            meta = {"role": (u.role if u else None), "cosmetics": _cosmetics_of(u) if u else None}
            cache[uid] = meta
            return meta
        msgs = []
        for m in reversed(rows):
            meta = await _meta(m.user_id)
            msgs.append({
                "user_id": m.user_id,
                "name": m.name,
                "role": meta["role"],
                "cosmetics": meta["cosmetics"],
                "text": m.text,
                "ts": m.created_at.isoformat() if m.created_at else None,
            })
        return {"messages": msgs}


# ---------- Game results (server-side) ----------

async def record_result_server(room_code: str, winner_id: int | None, winner_name: str | None, players: list,
                               started_at=None, duration_sec: int | None = None,
                               draw_count: int | None = None, turn_count: int | None = None):
    """Called by the WS layer exactly once per finished game.
    players: [{id, name, place, cards_left}] — place 1 = winner.
    XP: 1st +25, 2nd +12, 3rd +7, others +3.
    """
    xp_by_place = {1: 25, 2: 12, 3: 7}
    async with async_session_maker() as db:
        rec = GameRecord(
            room_code=room_code,
            winner_id=winner_id,
            winner_name=winner_name,
            players=players,
            started_at=started_at,
            duration_sec=duration_sec,
            draw_count=draw_count,
            turn_count=turn_count,
        )
        db.add(rec)
        for p in players:
            pid = p.get("id")
            if pid is None:
                continue
            u = await db.get(OnlineUser, pid)
            if not u:
                u = OnlineUser(id=pid, first_name=p.get("name"))
                db.add(u)
            elif p.get("name"):
                u.first_name = u.first_name or p.get("name")
            u.games_played = (u.games_played or 0) + 1
            place = p.get("place") or 99
            if place == 1:
                u.wins = (u.wins or 0) + 1
            else:
                u.losses = (u.losses or 0) + 1
            u.xp = (u.xp or 0) + xp_by_place.get(place, 3)
            u.level = 1 + (u.xp or 0) // 50
            # Coin rewards: 1st +50, 2nd +25, 3rd +15, others +5
            coin_by_place = {1: 50, 2: 25, 3: 15}
            u.coins = (u.coins or 0) + coin_by_place.get(place, 5)
        await db.commit()
    try:
        await _broadcast({"type": "panel_refresh", "scope": "games"})
    except Exception:
        pass


# ---------- Game results (client fallback, idempotent per room) ----------

@router.post("/games/result")
async def record_game_result(req: GameResultIn, session=Depends(get_session)):
    """Client fallback when WS game_over was missed (idempotent per room_code)."""
    async with session() as db:
        # Dedup: skip if this room already has a record
        existing = (await db.execute(
            select(GameRecord).where(GameRecord.room_code == req.room_code)
            .order_by(GameRecord.finished_at.desc())
        )).scalars().first()
        if existing:
            return {"ok": True, "duplicate": True}
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
                u.coins = (u.coins or 0) + 50
            else:
                u.losses = (u.losses or 0) + 1
                u.xp = (u.xp or 0) + 5
                u.coins = (u.coins or 0) + 5
            u.level = 1 + (u.xp or 0) // 50

        await db.commit()
    try:
        await _broadcast({"type": "panel_refresh", "scope": "games"})
    except Exception:
        pass
    return {"ok": True}


# ---------- Owner panel (only role='owner') ----------

async def _require_owner(user_id: int, db) -> OnlineUser:
    u = await db.get(OnlineUser, user_id)
    if not u or u.role not in ("main_owner", "co_owner", "owner"):
        raise HTTPException(403, "Faqat owner darajasidagi rollar uchun")
    return u


async def _expire_blocks(db):
    """Lift expired timed blocks (called on hot paths; cheap indexed update)."""
    from sqlalchemy import update
    await db.execute(update(OnlineUser)
                     .where(OnlineUser.blocked == 1,
                            OnlineUser.blocked_until.isnot(None),
                            OnlineUser.blocked_until <= now_local())
                     .values(blocked=0, blocked_until=None))


async def _is_blocked(db, user_id: int) -> bool:
    u = await db.get(OnlineUser, user_id)
    if not u or not u.blocked:
        return False
    if u.blocked_until and u.blocked_until <= now_local():
        u.blocked = 0
        u.blocked_until = None
        await db.commit()
        return False
    return True


async def _broadcast_stats_update(db, user_id: int):
    """Push fresh user info to all WebApp clients via global WebSocket.
    Targets update their profile/tags live, without a reload."""
    u = await db.get(OnlineUser, user_id)
    if not u:
        return
    from .fun import _equipped_market
    await _broadcast({
        "type": "stats_update",
        "user_id": user_id,
        "user": _user_public(u),
        "cosmetics": {kind: (item["url"] if item else None) for kind, item in _equipped_market(u).items()},
    })


@router.get("/owner/check/{user_id}")
async def owner_check(user_id: int, session=Depends(get_session)):
    """Frontend asks this to decide which panel button to show (owner/admin)."""
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        return {
            "is_owner": bool(u and u.role in ("main_owner", "co_owner", "owner")),
            "is_co_owner": bool(u and u.role == "co_owner"),
            "is_admin": bool(u and u.role in ("main_owner", "co_owner", "owner", "moderator", "deputy", "admin")),
            "is_moderator": bool(u and u.role in ("main_owner", "co_owner", "owner", "moderator", "deputy", "admin")),
            "is_main_owner": bool(u and u.role == "main_owner"),
            "role": (u.role if u else None),
        }


@router.get("/owner/whoami/{user_id}")
async def owner_whoami(user_id: int, session=Depends(get_session)):
    """Frontend guard: server bir xil javob, client so'rov yuborishdan oldin tekshiradi."""
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        role = (u.role if u else None)
        return {
            "is_main_owner": role == "main_owner",
            "is_co_owner": role == "co_owner",
            "is_owner": role in ("main_owner", "co_owner", "owner"),
            "is_admin": role in ("main_owner", "co_owner", "owner", "admin"),
            "is_deputy": role in ("moderator", "deputy"),
            "role": role,
        }


@router.get("/owner/stats")
async def owner_stats(owner_id: int, session=Depends(get_session)):
    async with session() as db:
        await _require_owner(owner_id, db)
        users = (await db.execute(select(func.count()).select_from(OnlineUser))).scalar() or 0
        online = (await db.execute(
            select(func.count()).select_from(OnlineUser)
            .where(OnlineUser.last_online >= now_local() - ONLINE_WINDOW)
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
                    "coins": u.coins or 0,
                    "likes": u.likes or 0,
                    "cosmetics": _cosmetics_of(u),
                }
                for u in rows
            ]
        }


class OwnerDeleteIn(BaseModel):
    owner_id: int
    user_id: int


@router.delete("/owner/users/{user_id}")
async def owner_delete_user(user_id: int, owner_id: int, session=Depends(get_session)):
    """Delete a user (and their stats); main_owner may delete anyone else,
    other owners only strictly lower roles."""
    async with session() as db:
        actor = await _require_owner(owner_id, db)
        if user_id == owner_id:
            raise HTTPException(400, "O'zingizni o'chira olmaysiz")
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not _can_manage(actor, u):
            raise HTTPException(403, "Bu foydalanuvchini o'chirish huquqi yo'q")
        # remove their game history entries (players is JSON with ids)
        records = (await db.execute(select(GameRecord))).scalars().all()
        for r in records:
            if any(p.get("id") == user_id for p in (r.players or [])):
                await db.delete(r)
        deleted_name = _display_name(u)
        await db.delete(u)
        await db.commit()
        await _broadcast({"type": "user_deleted", "user_id": user_id, "name": deleted_name})
        await _modlog(db, actor, "delete", f"🗑 {deleted_name} (ID {user_id}) o'chirildi", target_id=user_id)
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
        "ts": now_local().isoformat(),
    })
    return {"ok": True}


# ---------- Main Owner: extended facilities ----------

@router.get("/owner/users/{user_id}/detail")
async def owner_user_detail(user_id: int, owner_id: int, session=Depends(get_session)):
    """Full profile for the panel: stats, block info, friends, personal game history."""
    async with session() as db:
        await _require_owner(owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        now = now_local()
        online = bool(u.last_online and u.last_online >= now - ONLINE_WINDOW)
        rows = (await db.execute(
            select(Friendship).where(
                ((Friendship.user_id == user_id) | (Friendship.friend_id == user_id))
                & (Friendship.status == "accepted")
            )
        )).scalars().all()
        friend_ids = [f.friend_id if f.user_id == user_id else f.user_id for f in rows]
        friends = []
        for fid in friend_ids[:60]:
            f = await db.get(OnlineUser, fid)
            if f:
                friends.append({
                    "id": f.id,
                    "name": _display_name(f),
                    "online": bool(f.last_online and f.last_online >= now - ONLINE_WINDOW),
                })
        history = (await db.execute(
            select(GameRecord).order_by(GameRecord.finished_at.desc()).limit(100)
        )).scalars().all()
        games, wins, losses = [], 0, 0
        for r in history:
            players = r.players or []
            idx = next((i for i, p in enumerate(players) if p.get("id") == user_id), None)
            if idx is None:
                continue
            won = (r.winner_id == user_id)
            if won:
                wins += 1
            else:
                losses += 1
            games.append({
                "room_code": r.room_code,
                "place": idx + 1,
                "won": won,
                "players": [p.get("name") for p in players],
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            })
        return {
            "id": u.id,
            "name": _display_name(u),
            "first_name": u.first_name,
            "username": u.username,
            "nickname": u.nickname,
            "role": u.role or "player",
            "blocked": bool(u.blocked),
            "blocked_until": u.blocked_until.isoformat() if u.blocked_until else None,
            "block_reason": u.block_reason,
            "online": online,
            "last_online": u.last_online.isoformat() if u.last_online else None,
            "games_played": u.games_played,
        "wins": u.wins,
        "losses": u.losses,
        "xp": u.xp,
        "level": u.level,
        "coins": u.coins or 0,
        "friends": friends,
        "games": games[:20],
    }


class NickResetIn(BaseModel):
    owner_id: int


@router.post("/owner/users/{user_id}/reset-nickname")
async def owner_reset_nickname(user_id: int, req: NickResetIn, session=Depends(get_session)):
    """Main Owner: clear a user's nickname (back to Telegram name)."""
    async with session() as db:
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not _can_manage(actor, u):
            raise HTTPException(403, "Huquq yo'q")
        u.nickname = None
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        return {"ok": True, "name": _display_name(u)}


class KickIn(BaseModel):
    owner_id: int
    room_code: str
    reason: str | None = None


@router.post("/owner/users/{user_id}/disconnect")
async def owner_force_disconnect(user_id: int, req: KickIn, session=Depends(get_session)):
    """Main Owner: kick a user from a room/game and show them a notice."""
    async with session() as db:
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not _can_manage(actor, u):
            raise HTTPException(403, "Bu userga bu amal qilib bo'lmaydi")
        from ..main import manager
        removed = await manager.leave_all(user_id, reason="owner_kick")
        await manager.send_to_user(
            user_id,
            {"type": "owner_kick", "reason": (req.reason or "Siz admin tomonidan o'yindan chiqarildingiz")[:140]},
        )
        return {"ok": True, "rooms_left": removed}


# ---------- Owner user management actions ----------

class BlockIn(BaseModel):
    owner_id: int
    blocked: bool
    hours: float | None = None  # muddatli blok (soat); None/0 = permanent
    reason: str | None = None


@router.post("/owner/users/{user_id}/block")
async def owner_block_user(user_id: int, req: BlockIn, session=Depends(get_session)):
    """Block/unblock: strictly-lower roles only (main_owner everyone); timed blocks supported."""
    async with session() as db:
        await _expire_blocks(db)
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not _owner_action(actor, u):
            raise HTTPException(403, "Bu foydalanuvchini bloklash huquqi yo'q")
        if req.blocked:
            u.blocked = 1
            if req.hours and req.hours > 0:
                u.blocked_until = now_local() + timedelta(hours=req.hours)
            else:
                u.blocked_until = None
            u.block_reason = (req.reason or "Qoidabuzarlik")[:140]
        else:
            u.blocked = 0
            u.blocked_until = None
            u.block_reason = None
        await db.commit()
        await _broadcast({
            "type": "user_blocked",
            "user_id": user_id,
            "blocked": bool(u.blocked),
            "until": u.blocked_until.isoformat() if u.blocked_until else None,
            "reason": u.block_reason,
        })
        await _broadcast_stats_update(db, user_id)
        await _modlog(
            db, actor, "unblock" if not u.blocked else "block",
            ("🚫 " if u.blocked else "✅ ") + _display_name(u) + (f" — {u.block_reason}" if u.blocked else " blokdan olindi"),
            target_id=user_id,
        )
        return {"ok": True, "blocked": bool(u.blocked), "blocked_until": u.blocked_until.isoformat() if u.blocked_until else None}


class XpIn(BaseModel):
    owner_id: int
    amount: int


@router.post("/owner/users/{user_id}/xp")
async def owner_add_xp(user_id: int, req: XpIn, session=Depends(get_session)):
    """Add (or subtract with negative amount) XP; level is recalculated."""
    async with session() as db:
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not (_owner_action(actor, u) or user_id == req.owner_id):
            raise HTTPException(403, "Bu userga amal qilib bo'lmaydi")
        u.xp = max(0, (u.xp or 0) + req.amount)
        u.level = 1 + (u.xp or 0) // 50
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        await _modlog(db, actor, "xp", f"➕ XP {req.amount:+d} → {_display_name(u)}", target_id=user_id)
        return {"ok": True, "xp": u.xp, "level": u.level}


class CoinsIn(BaseModel):
    owner_id: int
    amount: int


@router.post("/owner/users/{user_id}/coins")
async def owner_add_coins(user_id: int, req: CoinsIn, session=Depends(get_session)):
    """Add/subtract coins (owner+)."""
    async with session() as db:
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not (_owner_action(actor, u) or user_id == req.owner_id):
            raise HTTPException(403, "Bu userga amal qilib bo'lmaydi")
        u.coins = max(0, (u.coins or 0) + req.amount)
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        await _modlog(db, actor, "coins", f"🪙 {req.amount:+d} → {_display_name(u)}", target_id=user_id)
        return {"ok": True, "coins": u.coins}


class LevelIn(BaseModel):
    owner_id: int
    level: int


@router.post("/owner/users/{user_id}/level")
async def owner_set_level(user_id: int, req: LevelIn, session=Depends(get_session)):
    """Set level directly; XP is aligned so future games keep the level."""
    level = max(1, req.level)
    async with session() as db:
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not (_owner_action(actor, u) or user_id == req.owner_id):
            raise HTTPException(403, "Bu userga amal qilib bo'lmaydi")
        u.level = level
        u.xp = (level - 1) * 50
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        await _modlog(db, actor, "level", f"🎚 Lvl {level} → {_display_name(u)}", target_id=user_id)
        return {"ok": True, "xp": u.xp, "level": u.level}


class RoleIn(BaseModel):
    owner_id: int
    role: str  # "owner" | "admin" | "deputy" | "player"


@router.post("/owner/users/{user_id}/role")
async def owner_set_role(user_id: int, req: RoleIn, session=Depends(get_session)):
    """Assign a role tag (hierarchy enforced):
    - main_owner can grant ANY role on ANY user (except themselves)
    - owner can grant owner/admin/deputy/player, but may not demote another owner
    - admin can grant deputy/player only
    """
    role = (req.role or "player").lower()
    if role not in VALID_ROLES:
        raise HTTPException(400, "Role: main_owner | owner | admin | deputy | player")
    async with session() as db:
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if user_id == req.owner_id:
            raise HTTPException(400, "O'z rolingizni o'zgartira olmaysiz")
        if role == "main_owner":
            if actor.role != "main_owner":
                raise HTTPException(403, "Faqat MAIN OWNER main_owner o'rnatadi")
        elif not _can_manage(actor, u):
            raise HTTPException(403, "Bu user rolini o'zgartirish huquqi yo'q")
        if u.role == "owner" and role not in ("main_owner", "co_owner", "owner") and actor.role not in ("main_owner", "co_owner"):
            raise HTTPException(400, "Ownerni faqat MAIN OWNER/CO-OWNER pasaytiradi")
        if role == "co_owner":
            if actor.role not in ("main_owner",):
                raise HTTPException(403, "Faqat MAIN OWNER co_owner o'rnatadi")
        elif role == "owner" and actor.role not in ("main_owner", "co_owner"):
            raise HTTPException(403, "Faqat MO/co_owner/owner owner o'rnatadi")
        elif role == "moderator" and actor.role not in ("main_owner", "co_owner", "owner"):
            raise HTTPException(403, "Faqat MO/co_owner/owner moderator o'rnatadi")
        elif role == "deputy" and actor.role not in ("main_owner", "co_owner", "owner"):
            raise HTTPException(403, "Faqat MO/co_owner/owner deputy o'rnatadi")
        elif role == "admin" and actor.role not in ("main_owner", "co_owner", "owner", "moderator", "deputy"):
            raise HTTPException(403, "Faqat MO/co_owner/owner/moderator/deputy admin o'rnatadi")
        old_role = u.role or "player"
        u.role = None if role == "player" else role
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        await _modlog(db, actor, "role", f"🛡 {old_role} → {role}: {_display_name(u)}", target_id=user_id)
        return {"ok": True, "role": u.role or "player"}


class StatsIn(BaseModel):
    owner_id: int
    games: int | None = None
    wins: int | None = None
    losses: int | None = None
    xp: int | None = None


# ---------- Owner: rooms / chat / games management ----------

@router.get("/owner/rooms")
async def owner_rooms(owner_id: int, session=Depends(get_session)):
    """All rooms with live player counts (owner can close them)."""
    from ..models.room import Room
    async with session() as db:
        await _require_owner(owner_id, db)
        rows = (
            await db.execute(select(Room).order_by(Room.created_at.desc()).limit(100))
        ).scalars().all()
        return {
            "rooms": [
                {
                    "code": r.id,
                    "host_id": r.host_id,
                    "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                    "players": len(r.player_ids or []),
                    "max_players": r.max_players,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        }


@router.delete("/owner/rooms/{code}")
async def owner_close_room(code: str, owner_id: int, session=Depends(get_session)):
    """Force-close a room and notify everyone inside."""
    from ..models.room import Room
    async with session() as db:
        await _require_owner(owner_id, db)
        r = await db.get(Room, code)
        if not r:
            raise HTTPException(404, "Xona topilmadi")
        await db.delete(r)
        await db.commit()
    from ..main import manager
    await manager.broadcast(code, {"type": "room_closed"})
    await _broadcast({"type": "room_deleted", "code": code})
    await _broadcast({"type": "panel_refresh", "scope": "rooms"})
    return {"ok": True}


@router.get("/owner/chat")
async def owner_chat(owner_id: int, session=Depends(get_session)):
    """Last 100 chat messages for moderation."""
    async with session() as db:
        await _require_owner(owner_id, db)
        rows = (
            await db.execute(
                select(ChatMessage).order_by(ChatMessage.created_at.desc()).limit(100)
            )
        ).scalars().all()
        return {
            "messages": [
                {
                    "id": m.id,
                    "user_id": m.user_id,
                    "name": m.name,
                    "text": m.text,
                    "ts": m.created_at.isoformat() if m.created_at else None,
                }
                for m in rows
            ]
        }


@router.delete("/owner/chat/{msg_id}")
async def owner_delete_chat(msg_id: int, owner_id: int, session=Depends(get_session)):
    """Single message delete — owner uchun ham moderator guard bilan."""
    async with session() as db:
        actor = await _require_moderator(owner_id, db)
        m = await db.get(ChatMessage, msg_id)
        if not m:
            raise HTTPException(404, "Xabar topilmadi")
        await db.delete(m)
        await db.commit()
        await _broadcast({"type": "chat_deleted", "id": msg_id})
        await _modlog(db, actor, "delete-msg", f"💬 xabar #{msg_id} o'chirildi ({m.name})")
    return {"ok": True}


@router.delete("/owner/chat")
async def owner_clear_chat(owner_id: int, session=Depends(get_session)):
    """Wipe the whole global chat history."""
    from sqlalchemy import delete
    async with session() as db:
        await _require_owner(owner_id, db)
        await db.execute(delete(ChatMessage))
        await db.commit()
    await _broadcast({"type": "chat_cleared"})
    return {"ok": True}


@router.get("/owner/games")
async def owner_games(owner_id: int, session=Depends(get_session)):
    async with session() as db:
        await _require_owner(owner_id, db)
        rows = (
            await db.execute(
                select(GameRecord).order_by(GameRecord.finished_at.desc()).limit(50)
            )
        ).scalars().all()
        return {"games": [_record_public(r) for r in rows]}


@router.get("/owner/games/{game_id}/detail")
async def owner_game_detail(game_id: int, owner_id: int, session=Depends(get_session)):
    """Full game info for the panel: players with places, duration, activity."""
    async with session() as db:
        await _require_owner(owner_id, db)
        r = await db.get(GameRecord, game_id)
        if not r:
            raise HTTPException(404, "O'yin topilmadi")
        # Hozirgi user ma'lumotlari bilan boyitish (role/level/XP + cosmetics everywhere)
        enriched = []
        for p in (r.players or []):
            pid = p.get("id")
            u = await db.get(OnlineUser, pid) if pid is not None else None
            enriched.append({
                **p,
                "username": u.username if u else None,
                "role": (u.role if u else None),
                "level": (u.level if u else None),
                "xp": (u.xp if u else None),
                "cosmetics": _cosmetics_of(u) if u else None,
                "online": bool(u and u.last_online and (now_local() - u.last_online).total_seconds() < 60),
                "still_exists": bool(u),
            })
        return {
            "game": {
                **_record_public(r),
                "players_enriched": enriched,
            }
        }


@router.post("/owner/users/{user_id}/reset-stats")
async def owner_reset_stats(user_id: int, req: StatsIn, session=Depends(get_session)):
    """Zero out a user's statistics."""
    async with session() as db:
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not (_owner_action(actor, u) or user_id == req.owner_id):
            raise HTTPException(403, "Bu userga amal qilib bo'lmaydi")
        u.games_played = 0
        u.wins = 0
        u.losses = 0
        u.xp = 0
        u.level = 1
        await db.commit()
        await _broadcast_stats_update(db, user_id)
        await _modlog(db, actor, "reset", f"♻️ Statistika reset → {_display_name(u)}", target_id=user_id)
        return {"ok": True}


@router.post("/owner/users/{user_id}/stats")
async def owner_edit_stats(user_id: int, req: StatsIn, session=Depends(get_session)):
    """Overwrite statistics values; level is recalculated from XP."""
    async with session() as db:
        actor = await _require_owner(req.owner_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not (_owner_action(actor, u) or user_id == req.owner_id):
            raise HTTPException(403, "Bu userga amal qilib bo'lmaydi")
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
        await _modlog(db, actor, "stats", f"📊 Statistika tahrirlandi → {_display_name(u)}", target_id=user_id)
        return {"ok": True, "user": _user_public(u)}


# ---------- Admin panel (owner + admin) ----------

async def _require_admin(user_id: int, db) -> OnlineUser:
    u = await db.get(OnlineUser, user_id)
    if not u or u.role not in ("main_owner", "co_owner", "owner", "moderator", "deputy", "admin"):
        raise HTTPException(403, "Faqat admin darajasidagi rollar uchun")
    return u


async def _require_moderator(user_id: int, db) -> OnlineUser:
    """Barcha moderator darajasidagi rollar (MO > CO > Owner > Moderator > Deputy > Admin)."""
    u = await db.get(OnlineUser, user_id)
    if not u or u.role not in ("main_owner", "co_owner", "owner", "moderator", "deputy", "admin"):
        raise HTTPException(403, "Faqat moderator rollar uchun")
    return u


class MuteIn(BaseModel):
    admin_id: int
    muted: bool
    minutes: int | None = None  # muddatli mute; None/0 = doimiy
    reason: str | None = None


@router.post("/admin/users/{user_id}/mute")
async def admin_mute_user(user_id: int, req: MuteIn, session=Depends(get_session)):
    """Mute: user o'ynay oladi, lekin chat yozolmaydi (global + xonalar).
    Moderatorlar faqat pastdagi rollarga mute beradi."""
    async with session() as db:
        await _expire_blocks(db)
        actor = await _require_moderator(req.admin_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not _owner_action(actor, u):
            raise HTTPException(403, "Bu userga mute berish huquqi yo'q")
        if not req.muted:
            u.muted = 0
            u.muted_until = None
        else:
            u.muted = 1
            u.muted_until = (now_local() + timedelta(minutes=req.minutes)) if (req.minutes and req.minutes > 0) else None
        await db.commit()
        await _broadcast({"type": "user_muted", "user_id": user_id, "muted": bool(u.muted)})
        await _broadcast_stats_update(db, user_id)
        await _modlog(db, actor, "mute", (("🔇 MUTE — " if u.muted else "🔊 UNMUTE — ") + _display_name(u) + (f" ({req.reason})" if (u.muted and req.reason) else "")), target_id=user_id)
        return {"ok": True, "muted": bool(u.muted), "muted_until": u.muted_until.isoformat() if u.muted_until else None}


class WarnIn(BaseModel):
    admin_id: int
    text: str


@router.post("/admin/users/{user_id}/warn")
async def admin_warn_user(user_id: int, req: WarnIn, session=Depends(get_session)):
    """Warn: userga ogohlantirish yuboriladi (toast + saqlanadi, o'zi o'chiradi)."""
    async with session() as db:
        actor = await _require_moderator(req.admin_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not _owner_action(actor, u):
            raise HTTPException(403, "Bu userga ogohlantirish berish huquqi yo'q")
        text = (req.text or "").strip()[:220]
        if not text:
            raise HTTPException(400, "Ogohlantirish matni bo'sh")
        u.warning = text
        await db.commit()
        await _broadcast({"type": "user_warned", "user_id": user_id, "warning": text})
        await _modlog(db, actor, "warn", f"⚠️ WARN — {_display_name(u)}: {text}", target_id=user_id)
        return {"ok": True}


@router.post("/users/warn/ack")
async def warn_ack(req: UserIn, session=Depends(get_session)):
    """User ogohlantirishni ko'rib bo'lib yopadi."""
    async with session() as db:
        u = await db.get(OnlineUser, req.id)
        if u and u.warning:
            u.warning = None
            await db.commit()
    return {"ok": True}


@router.delete("/owner/games/{game_id}")
async def admin_delete_game(game_id: int, admin_id: int, session=Depends(get_session)):
    """Delete one game record — moderatorlar uchun."""
    async with session() as db:
        actor = await _require_moderator(admin_id, db)
        r = await db.get(GameRecord, game_id)
        if not r:
            raise HTTPException(404, "O'yin topilmadi")
        await db.delete(r)
        await db.commit()
        await _modlog(db, actor, "delete-game", f"🎮 o'yin #{game_id} ({r.room_code}) o'chirildi")
    return {"ok": True}


# ---------- Main Owner extended facilities ----------

@router.get("/owner/cleanup")
async def owner_cleanup(owner_id: int, session=Depends(get_session)):
    """Remove stale lobbies (>30 min) and orphaned FINISHED rooms; returns what was removed."""
    async with session() as db:
        actor = await _require_owner(owner_id, db)
        removed = await _cleanup_rooms(db)
        await _modlog(db, actor, "cleanup", f"🧹 Tozalash: {removed} ta o'lik xona o'chirildi")
        return {"ok": True, "removed": removed}


@router.get("/owner/tournaments")
async def owner_tournaments(owner_id: int, session=Depends(get_session)):
    """Barcha turnirlar (open/running/finished) — MO boshqaruvi uchun."""
    async with session() as db:
        await _require_owner(owner_id, db)
        rows = (await db.execute(
            select(Tournament).order_by(Tournament.created_at.desc()).limit(50)
        )).scalars().all()
        out = []
        for t in rows:
            w = await db.get(OnlineUser, t.winner_id) if t.winner_id else None
            out.append({
                "code": t.code, "status": t.status, "entry_fee": t.entry_fee,
                "prize": t.prize or 0, "players": len(t.players or []),
                "winner_id": t.winner_id, "winner_name": _display_name(w) if w else None,
                "host_id": t.host_id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            })
        return {"tournaments": out}


async def _owner_tournament(code: str, owner_id: int, db) -> Tournament:
    t = (await db.execute(select(Tournament).where(Tournament.code == code))).scalars().first()
    if not t:
        raise HTTPException(404, "Turnir topilmadi")
    return t


@router.post("/owner/tournaments/{code}/start")
async def owner_tournament_start(code: str, owner_id: int, session=Depends(get_session)):
    """MO turnirni host o'rniga boshlaydi."""
    import random
    async with session() as db:
        actor = await _require_owner(owner_id, db)
        t = await _owner_tournament(code, owner_id, db)
        if t.status != "open":
            raise HTTPException(400, "Turnir allaqachon boshlangan")
        ps = t.players or []
        if len(ps) < 2:
            raise HTTPException(400, "Kamida 2 o'yinchi kerak")
        random.shuffle(ps)
        ids = [p["id"] for p in ps]
        t.matches = [{"round": 1, "p1": ids[i], "p2": ids[i + 1], "winner_id": None}
                     for i in range(0, len(ids) - 1, 2)]
        t.status = "running"
        await db.commit()
        await _broadcast({"type": "panel_refresh", "scope": "tournaments"})
        await _modlog(db, actor, "tournament", f"▶️ Turnir {code} boshlatildi ({len(ps)} o'yinchi)")
        return {"ok": True, "matches": t.matches}


@router.post("/owner/tournaments/{code}/cancel")
async def owner_tournament_cancel(code: str, owner_id: int, session=Depends(get_session)):
    """MO turnirni bekor qiladi — kirish coinlari qaytariladi."""
    async with session() as db:
        actor = await _require_owner(owner_id, db)
        t = await _owner_tournament(code, owner_id, db)
        if t.status == "finished":
            raise HTTPException(400, "Tugagan turnir bekor qilinmaydi")
        refunded = 0
        if t.entry_fee > 0:
            for p in (t.players or []):
                u = await db.get(OnlineUser, p.get("id"))
                if u:
                    u.coins = (u.coins or 0) + t.entry_fee
                    await _broadcast_stats_update(db, u.id)
                    refunded += 1
        t.status = "cancelled"
        t.finished_at = now_local()
        await db.commit()
        await _broadcast({"type": "panel_refresh", "scope": "tournaments"})
        await _modlog(db, actor, "tournament", f"✖️ Turnir {code} bekor qilindi ({refunded} ta refund)")
        return {"ok": True, "refunded": refunded}


@router.post("/owner/tournaments/{code}/finish")
async def owner_tournament_finish(code: str, owner_id: int, session=Depends(get_session)):
    """MO turnirni shu zahoti tugatadi; aniq g'olib bo'lsa prize beriladi."""
    async with session() as db:
        actor = await _require_owner(owner_id, db)
        t = await _owner_tournament(code, owner_id, db)
        if t.status != "running":
            raise HTTPException(400, "Faol turnirgina tugatiladi")
        # Aniq g'olib: final matchda winner bo'lsa shu
        winner_id = None
        if t.matches:
            last_round = max(m["round"] for m in t.matches)
            finals = [m for m in t.matches if m["round"] == last_round]
            if len(finals) == 1 and finals[0]["winner_id"]:
                winner_id = finals[0]["winner_id"]
        t.status = "finished"
        t.winner_id = winner_id or t.winner_id
        t.finished_at = now_local()
        winner_name = None
        if t.winner_id and (t.prize or 0) > 0:
            w = await db.get(OnlineUser, t.winner_id)
            if w:
                w.coins = (w.coins or 0) + (t.prize or 0)
                winner_name = _display_name(w)
                await _broadcast_stats_update(db, w.id)
        await db.commit()
        await _broadcast({"type": "panel_refresh", "scope": "tournaments"})
        await _modlog(db, actor, "tournament", f"🏁 Turnir {code} tugatildi (g'olib: {winner_name or 'aniqlanmagan'})")
        return {"ok": True, "winner_id": t.winner_id, "winner_name": winner_name}


@router.delete("/owner/tournaments/{code}")
async def owner_tournament_delete(code: str, owner_id: int, session=Depends(get_session)):
    """MO turnirni butunlay o'chiradi (faqat main_owner)."""
    async with session() as db:
        actor = await _require_owner(owner_id, db)
        if actor.role != "main_owner":
            raise HTTPException(403, "Faqat MAIN OWNER turnirni o'chiradi")
        t = await _owner_tournament(code, owner_id, db)
        await db.delete(t)
        await db.commit()
        await _broadcast({"type": "panel_refresh", "scope": "tournaments"})
        await _modlog(db, actor, "tournament", f"🗑 Turnir {code} butunlay o'chirildi")
        return {"ok": True}


@router.get("/owner/modlog")
async def owner_modlog(owner_id: int, session=Depends(get_session)):
    """Latest moderation actions (log) — panel shows the last 80."""
    async with session() as db:
        await _require_owner(owner_id, db)
        rows = (await db.execute(
            select(ModLogEntry).order_by(ModLogEntry.created_at.desc()).limit(80)
        )).scalars().all()
        return {"log": [
            {
                "id": e.id,
                "actor": e.actor_name,
                "action": e.action,
                "text": e.text,
                "ts": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]}


@router.get("/owner/ws")
async def owner_ws(owner_id: int, session=Depends(get_session)):
    """Live view of room sockets (join the panel's O'yinlar tab to observe the game server)."""
    async with session() as db:
        await _require_owner(owner_id, db)
    from ..main import manager, games
    room_list = []
    for code, conns in manager.active_connections.items():
        if code == "global":
            continue
        g = games.get(code)
        room_list.append({
            "code": code,
            "connections": len(conns),
            "players": sorted(manager.user_sockets.get(code, {}).keys()),
            "in_game": bool(g and getattr(g, "winner_id", None) is None),
        })
    return {"rooms": room_list, "total_connections": sum(len(c) for k, c in manager.active_connections.items() if k != "global")}
    u = await db.get(OnlineUser, user_id)
    if not u or u.role != "main_owner":
        raise HTTPException(403, "Faqat main_owner uchun")
    return u


@router.get("/admin/stats")
async def admin_stats(admin_id: int, session=Depends(get_session)):
    """Limited counters for the admin/deputy panel."""
    async with session() as db:
        await _require_moderator(admin_id, db)
        users = (await db.execute(select(func.count()).select_from(OnlineUser))).scalar() or 0
        online = (await db.execute(
            select(func.count()).select_from(OnlineUser)
            .where(OnlineUser.last_online >= now_local() - ONLINE_WINDOW)
        )).scalar() or 0
        games = (await db.execute(select(func.count()).select_from(GameRecord))).scalar() or 0
        return {"users": users, "online": online, "games": games}


@router.get("/admin/users")
async def admin_users(admin_id: int, session=Depends(get_session)):
    async with session() as db:
        await _require_moderator(admin_id, db)
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
                    "muted": bool(u.muted),
                    "games": u.games_played,
                    "wins": u.wins,
                    "losses": u.losses,
                    "xp": u.xp,
                    "level": u.level,
                    "coins": u.coins or 0,
                    "likes": u.likes or 0,
                    "cosmetics": _cosmetics_of(u),
                    "last_online": u.last_online.isoformat() if u.last_online else None,
                }
                for u in rows
            ]
        }


class AdminBlockIn(BaseModel):
    admin_id: int
    blocked: bool
    hours: float | None = None  # muddatli blok (soat)
    reason: str | None = None


@router.post("/admin/users/{user_id}/block")
async def admin_block_user(user_id: int, req: AdminBlockIn, session=Depends(get_session)):
    """owner/admin/deputy may block strictly lower-ranked users (timed blocks ok)."""
    async with session() as db:
        await _expire_blocks(db)
        actor = await _require_moderator(req.admin_id, db)
        u = await db.get(OnlineUser, user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if req.blocked and not _can_manage(actor, u):
            raise HTTPException(403, "Bu foydalanuvchini bloklash huquqi yo'q")
        if req.blocked:
            u.blocked = 1
            if req.hours and req.hours > 0:
                u.blocked_until = now_local() + timedelta(hours=req.hours)
            else:
                u.blocked_until = None
            u.block_reason = (req.reason or "Qoidabuzarlik")[:140]
        else:
            u.blocked = 0
            u.blocked_until = None
            u.block_reason = None
        await db.commit()
        await _broadcast({
            "type": "user_blocked",
            "user_id": user_id,
            "blocked": bool(u.blocked),
            "until": u.blocked_until.isoformat() if u.blocked_until else None,
            "reason": u.block_reason,
        })
        await _broadcast_stats_update(db, user_id)
        await _modlog(
            db, actor, "unblock" if not u.blocked else "block",
            ("🚫 " if u.blocked else "✅ ") + _display_name(u) + (f" — {u.block_reason}" if u.blocked else " blokdan olindi"),
            target_id=user_id,
        )
        return {"ok": True, "blocked": bool(u.blocked), "blocked_until": u.blocked_until.isoformat() if u.blocked_until else None}
