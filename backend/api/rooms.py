# backend/api/rooms.py

"""REST API for room management.
- POST /rooms/create       -> creates a new room, returns room code
- POST /rooms/join/{code}  -> joins a user to an existing room
- GET  /rooms/{code}       -> returns room status (players list, host, etc.)
- POST /rooms/kick/{code}  -> host removes a player (host only)
- POST /rooms/leave/{code} -> player leaves; host role passes on, empty room is deleted
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
import string
import random
from datetime import datetime, timedelta

from ..models.room import Room, RoomStatus
from ..database import async_session_maker
from ..tz import now_local

async def get_session():
    """Yield the session factory so handlers can do `async with session() as db`.
    Needed because FastAPI misinterprets the async_sessionmaker class as a
    dependency class and injects its __init__ params (e.g. `local_kw`).
    """
    yield async_session_maker

router = APIRouter(prefix="/rooms", tags=["rooms"])

async def _broadcast(code: str, message: dict):
    """Lazy import avoids a circular import with backend.main."""
    from ..main import manager
    await manager.broadcast(code, message)

async def _broadcast_global(message: dict):
    """Panel live-refresh uchun global kanalga broadcast."""
    from .social import _broadcast as _gb
    await _gb(message)

def _normalize_code(code: str | None) -> str:
    """Xona kodini kanonik ko'rinishga keltirish (strip + upper)."""
    return (code or "").strip().upper()


def _generate_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))

class CreateRoomRequest(BaseModel):
    host_id: int
    max_players: int = 4  # 2..7 qabul qilinadi
    password: str | None = None  # bo'sh bo'lsa ochiq xona
    entry_fee: int = 0  # 0 = bepul, >0 = har bir o'yinchi shuncha coin to'laydi, g'olib hammasini oladi

class JoinRoomRequest(BaseModel):
    user_id: int
    password: str | None = None

class KickRequest(BaseModel):
    host_id: int
    target_id: int

class LeaveRequest(BaseModel):
    user_id: int

class RoomResponse(BaseModel):
    code: str
    host_id: int
    status: str
    players: list  # [{id, name}] - enriched with names when available
    max_players: int
    has_password: bool = False
    entry_fee: int = 0
    prize: int = 0


async def _enrich_players(db, player_ids: list) -> list:
    """Convert player id list to [{id, name, role, blocked, cosmetics}] using online_users."""
    from ..models.social import OnlineUser
    from .social import _cosmetics_of
    out = []
    for pid in player_ids:
        name = None
        role = None
        blocked = False
        cosmetics = None
        u = await db.get(OnlineUser, pid)
        if u:
            name = u.nickname or u.first_name or u.username
            role = u.role
            blocked = bool(u.blocked)
            try:
                cosmetics = _cosmetics_of(u)
            except Exception:
                cosmetics = None
        out.append({"id": pid, "name": name or f"O'yinchi {pid % 1000}", "role": role, "blocked": blocked, "cosmetics": cosmetics})
    return out

@router.post("/create", response_model=RoomResponse)
async def create_room(req: CreateRoomRequest, session=Depends(get_session)):
    entry_fee = max(0, min(int(req.entry_fee or 0), 500))
    async with session() as db:
        # coin tekshiruvi: kirish to'lovini host ham to'laydi
        if entry_fee > 0:
            from ..models.social import OnlineUser
            host = await db.get(OnlineUser, req.host_id)
            if host and (host.coins or 0) < entry_fee:
                raise HTTPException(status_code=400, detail=f"Coin yetmadi (kerak: {entry_fee})")
            if host:
                host.coins = (host.coins or 0) - entry_fee
        # generate unique code
        while True:
            code = _generate_code()
            existing = await db.get(Room, code)
            if not existing:
                break
        room = Room(
            id=code,
            host_id=req.host_id,
            max_players=max(2, min(7, int(req.max_players or 4))),
            status=RoomStatus.WAITING,
            player_ids=[req.host_id],
            password=(req.password or None),
            entry_fee=entry_fee,
            prize=entry_fee,  # host to'lovi
        )
        db.add(room)
        await db.commit()
        await db.refresh(room)
        if entry_fee > 0:
            try:
                from .social import _broadcast_stats_update as _bsu
                await _bsu(db, req.host_id)
            except Exception:
                pass
        try:
            await _broadcast_global({"type": "panel_refresh", "scope": "rooms"})
        except Exception:
            pass
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=await _enrich_players(db, room.player_ids),
            max_players=room.max_players,
            has_password=bool(room.password),
            entry_fee=room.entry_fee or 0,
            prize=room.prize or 0,
        )

@router.post("/join/{code}", response_model=RoomResponse)
async def join_room(code: str, req: JoinRoomRequest, session=Depends(get_session)):
    code = (code or "").strip().upper()  # case-insensitive kod kirish
    async with session() as db:
        room = await db.get(Room, code)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        # Reload/uzilishdan keyin qayta kirishda xato chiqarmasdan qaytaramiz
        if str(req.user_id) in [str(p) for p in (room.player_ids or [])]:
            enriched = await _enrich_players(db, room.player_ids)
            return RoomResponse(
                code=room.id, host_id=room.host_id, status=room.status.value,
                players=enriched, max_players=room.max_players, has_password=bool(room.password),
            )
        if len(room.player_ids) >= room.max_players:
            raise HTTPException(status_code=400, detail="Room is full")
        if req.user_id in room.player_ids:
            raise HTTPException(status_code=400, detail="User already in room")
        # Parolli xona: parol to'g'ri bo'lishi shart (host va bot taklifi bundan mustasno emas)
        if room.password and (req.password or "") != room.password:
            raise HTTPException(status_code=403, detail="Noto'g'ri parol")
        # Blocked users may not join rooms
        from ..models.social import OnlineUser
        bu = await db.get(OnlineUser, req.user_id)
        if bu and bu.blocked:
            raise HTTPException(status_code=403, detail="Siz bloklangansiz")
        # Rejimli xona: kirish to'lovi
        fee = getattr(room, 'entry_fee', 0) or 0
        if fee > 0:
            if (bu.coins or 0) < fee:
                raise HTTPException(status_code=400, detail=f"Coin yetmadi (kerak: {fee})")
            bu.coins = (bu.coins or 0) - fee
            room.prize = (getattr(room, 'prize', 0) or 0) + fee
        room.player_ids.append(req.user_id)
        # If enough players, status can move to READY automatically (optional)
        if len(room.player_ids) >= 2:
            room.status = RoomStatus.READY
        await db.commit()
        await db.refresh(room)
        if fee > 0:
            try:
                from .social import _broadcast_stats_update as _bsu2
                await _bsu2(db, req.user_id)
            except Exception:
                pass
        enriched = await _enrich_players(db, room.player_ids)
        await _broadcast(code, {
            "type": "player_joined",
            "room": {"code": room.id, "host_id": room.host_id, "players": enriched, "entry_fee": getattr(room, 'entry_fee', 0) or 0, "prize": getattr(room, 'prize', 0) or 0},
        })
        try:
            await _broadcast_global({"type": "panel_refresh", "scope": "rooms"})
        except Exception:
            pass
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=enriched,
            max_players=room.max_players,
            has_password=bool(room.password),
            entry_fee=getattr(room, 'entry_fee', 0) or 0,
            prize=getattr(room, 'prize', 0) or 0,
        )

@router.post("/leave/{code}", response_model=RoomResponse)
async def leave_room(code: str, req: LeaveRequest, session=Depends(get_session)):
    """Remove a user from a room.
    - Last player leaving deletes the room entirely (fixes ghost rooms).
    - If the host leaves, host role passes to the next remaining player.
    """
    async with session() as db:
        room = await db.get(Room, code)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")

        if req.user_id not in room.player_ids:
            # Already gone (e.g. double tap) - return current state instead of failing
            return RoomResponse(
                code=room.id,
                host_id=room.host_id,
                status=room.status.value,
                players=await _enrich_players(db, room.player_ids),
                max_players=room.max_players,
                has_password=bool(room.password),
                entry_fee=getattr(room, 'entry_fee', 0) or 0,
                prize=getattr(room, 'prize', 0) or 0,
            )

        was_host = room.host_id == req.user_id
        # Rejimli xona: o'yin boshlanmasdan chiqsa, to'lov qaytariladi
        fee = getattr(room, 'entry_fee', 0) or 0
        if fee > 0 and room.status in (RoomStatus.WAITING, RoomStatus.READY):
            from ..models.social import OnlineUser
            u = await db.get(OnlineUser, req.user_id)
            if u:
                u.coins = (u.coins or 0) + fee
                room.prize = max(0, (getattr(room, 'prize', 0) or 0) - fee)
                try:
                    from .social import _broadcast_stats_update as _bsu3
                    await _bsu3(db, req.user_id)
                except Exception:
                    pass
        room.player_ids.remove(req.user_id)

        if not room.player_ids:
            # Nobody left - close the room so it does not linger as a ghost
            await db.delete(room)
            await db.commit()
            await _broadcast(code, {"type": "room_closed"})
            try:
                await _broadcast_global({"type": "panel_refresh", "scope": "rooms"})
            except Exception:
                pass
            raise HTTPException(status_code=404, detail="Room closed")

        if was_host:
            # Host left: pass ownership to the next remaining player
            room.host_id = room.player_ids[0]

        await db.commit()
        await db.refresh(room)
        enriched = await _enrich_players(db, room.player_ids)
        await _broadcast(code, {
            "type": "room_left",
            "room": {"code": room.id, "host_id": room.host_id, "players": enriched, "entry_fee": getattr(room, 'entry_fee', 0) or 0, "prize": getattr(room, 'prize', 0) or 0},
        })
        try:
            await _broadcast_global({"type": "panel_refresh", "scope": "rooms"})
        except Exception:
            pass
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=enriched,
            max_players=room.max_players,
            has_password=bool(room.password),
            entry_fee=getattr(room, 'entry_fee', 0) or 0,
            prize=getattr(room, 'prize', 0) or 0,
        )

@router.post("/kick/{code}", response_model=RoomResponse)
async def kick_room(code: str, req: KickRequest, session=Depends(get_session)):
    """Host removes a player from the lobby (host only)."""
    async with session() as db:
        room = await db.get(Room, code)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        if req.host_id != room.host_id:
            raise HTTPException(status_code=403, detail="Only the host can kick players")
        if req.target_id == room.host_id:
            raise HTTPException(status_code=400, detail="Host cannot kick themselves")
        if req.target_id not in room.player_ids:
            raise HTTPException(status_code=404, detail="Player not in room")

        # kickda ham to'lov qaytariladi (rejimli bo'lsa)
        fee = getattr(room, 'entry_fee', 0) or 0
        if fee > 0 and room.status in (RoomStatus.WAITING, RoomStatus.READY):
            from ..models.social import OnlineUser
            u2 = await db.get(OnlineUser, req.target_id)
            if u2:
                u2.coins = (u2.coins or 0) + fee
                room.prize = max(0, (getattr(room, 'prize', 0) or 0) - fee)
                try:
                    from .social import _broadcast_stats_update as _bsu_k
                    await _bsu_k(db, req.target_id)
                except Exception:
                    pass
        room.player_ids.remove(req.target_id)
        await db.commit()
        await db.refresh(room)
        enriched = await _enrich_players(db, room.player_ids)
        await _broadcast(code, {
            "type": "player_kicked",
            "room": {"code": room.id, "host_id": room.host_id, "players": enriched, "entry_fee": getattr(room, 'entry_fee', 0) or 0, "prize": getattr(room, 'prize', 0) or 0},
            "kicked": req.target_id,
        })
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=enriched,
            max_players=room.max_players,
            has_password=bool(room.password),
            entry_fee=getattr(room, 'entry_fee', 0) or 0,
            prize=getattr(room, 'prize', 0) or 0,
        )

@router.get("/open", response_model=list[RoomResponse])
async def open_rooms(session=Depends(get_session)):
    """Public lobbies that are joinable (WAITING or READY, not full), last 2 hours."""
    from ..models.social import OnlineUser
    cutoff = now_local() - timedelta(hours=2)
    async with session() as db:
        rooms = (
            await db.execute(
                select(Room)
                .where(
                    Room.status.in_([RoomStatus.WAITING, RoomStatus.READY]),
                    Room.created_at >= cutoff,
                )
                .order_by(Room.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        out = []
        for r in rooms:
            out.append(RoomResponse(
                code=r.id,
                host_id=r.host_id,
                status=r.status.value,
                players=await _enrich_players(db, r.player_ids),
                max_players=r.max_players,
                has_password=bool(r.password),
                entry_fee=getattr(r, 'entry_fee', 0) or 0,
                prize=getattr(r, 'prize', 0) or 0,
            ))
        return out


@router.get("/{code}", response_model=RoomResponse)
async def get_room(code: str, session=Depends(get_session)):
    code = (code or "").strip().upper()
    async with session() as db:
        room = await db.get(Room, code)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=await _enrich_players(db, room.player_ids),
            max_players=room.max_players,
            has_password=bool(room.password),
            entry_fee=getattr(room, 'entry_fee', 0) or 0,
            prize=getattr(room, 'prize', 0) or 0,
        )
