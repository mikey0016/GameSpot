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
from sqlalchemy import select
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
        "last_online": u.last_online.isoformat() if u.last_online else None,
    }


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
        return {"ok": True, "invites": pending}


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
            "games_played": u.games_played,
            "wins": u.wins,
            "losses": u.losses,
            "xp": u.xp,
            "level": u.level,
        }


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
                    "name": u.first_name or u.username or "O'yinchi",
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
