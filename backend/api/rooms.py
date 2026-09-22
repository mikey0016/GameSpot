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

def _generate_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))

class CreateRoomRequest(BaseModel):
    host_id: int
    max_players: int = 4

class JoinRoomRequest(BaseModel):
    user_id: int

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


async def _enrich_players(db, player_ids: list) -> list:
    """Convert player id list to [{id, name, role, blocked}] using online_users."""
    from ..models.social import OnlineUser
    out = []
    for pid in player_ids:
        name = None
        role = None
        blocked = False
        u = await db.get(OnlineUser, pid)
        if u:
            name = u.nickname or u.first_name or u.username
            role = u.role
            blocked = bool(u.blocked)
        out.append({"id": pid, "name": name or f"O'yinchi {pid % 1000}", "role": role, "blocked": blocked})
    return out

@router.post("/create", response_model=RoomResponse)
async def create_room(req: CreateRoomRequest, session=Depends(get_session)):
    async with session() as db:
        # generate unique code
        while True:
            code = _generate_code()
            existing = await db.get(Room, code)
            if not existing:
                break
        room = Room(
            id=code,
            host_id=req.host_id,
            max_players=req.max_players,
            status=RoomStatus.WAITING,
            player_ids=[req.host_id],
        )
        db.add(room)
        await db.commit()
        await db.refresh(room)
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=await _enrich_players(db, room.player_ids),
            max_players=room.max_players,
        )

@router.post("/join/{code}", response_model=RoomResponse)
async def join_room(code: str, req: JoinRoomRequest, session=Depends(get_session)):
    async with session() as db:
        room = await db.get(Room, code)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        if len(room.player_ids) >= room.max_players:
            raise HTTPException(status_code=400, detail="Room is full")
        if req.user_id in room.player_ids:
            raise HTTPException(status_code=400, detail="User already in room")
        # Blocked users may not join rooms
        from ..models.social import OnlineUser
        bu = await db.get(OnlineUser, req.user_id)
        if bu and bu.blocked:
            raise HTTPException(status_code=403, detail="Siz bloklangansiz")
        room.player_ids.append(req.user_id)
        # If enough players, status can move to READY automatically (optional)
        if len(room.player_ids) >= 2:
            room.status = RoomStatus.READY
        await db.commit()
        await db.refresh(room)
        enriched = await _enrich_players(db, room.player_ids)
        await _broadcast(code, {
            "type": "player_joined",
            "room": {"code": room.id, "host_id": room.host_id, "players": enriched},
        })
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=enriched,
            max_players=room.max_players,
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
            )

        was_host = room.host_id == req.user_id
        room.player_ids.remove(req.user_id)

        if not room.player_ids:
            # Nobody left - close the room so it does not linger as a ghost
            await db.delete(room)
            await db.commit()
            await _broadcast(code, {"type": "room_closed"})
            raise HTTPException(status_code=404, detail="Room closed")

        if was_host:
            # Host left: pass ownership to the next remaining player
            room.host_id = room.player_ids[0]

        await db.commit()
        await db.refresh(room)
        enriched = await _enrich_players(db, room.player_ids)
        await _broadcast(code, {
            "type": "room_left",
            "room": {"code": room.id, "host_id": room.host_id, "players": enriched},
        })
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=enriched,
            max_players=room.max_players,
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

        room.player_ids.remove(req.target_id)
        await db.commit()
        await db.refresh(room)
        enriched = await _enrich_players(db, room.player_ids)
        await _broadcast(code, {
            "type": "player_kicked",
            "room": {"code": room.id, "host_id": room.host_id, "players": enriched},
            "kicked": req.target_id,
        })
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=enriched,
            max_players=room.max_players,
        )

@router.get("/open", response_model=list[RoomResponse])
async def open_rooms(session=Depends(get_session)):
    """Public lobbies that are still WAITING for players (created in the last 30 min)."""
    from ..models.social import OnlineUser
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    async with session() as db:
        rooms = (
            await db.execute(
                select(Room)
                .where(
                    Room.status == RoomStatus.WAITING,
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
            ))
        return out


@router.get("/{code}", response_model=RoomResponse)
async def get_room(code: str, session=Depends(get_session)):
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
        )
