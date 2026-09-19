# backend/api/rooms.py

"""REST API for room management.
- POST /rooms/create  -> creates a new room, returns room code
- POST /rooms/join/{code} -> joins a user to an existing room
- GET  /rooms/{code} -> returns room status (players list, ready states, etc.)
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
import string
import random

from ..models.room import Room, RoomStatus
from ..database import async_session_maker
from ..config import settings

router = APIRouter(prefix="/rooms", tags=["rooms"])

def _generate_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))

class CreateRoomRequest(BaseModel):
    host_id: int
    max_players: int = 4

class JoinRoomRequest(BaseModel):
    user_id: int

class RoomResponse(BaseModel):
    code: str
    host_id: int
    status: str
    players: list[int]
    max_players: int

@router.post("/create", response_model=RoomResponse)
async def create_room(req: CreateRoomRequest, session=Depends(async_session_maker)):
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
            players=room.player_ids,
            max_players=room.max_players,
        )

@router.post("/join/{code}", response_model=RoomResponse)
async def join_room(code: str, req: JoinRoomRequest, session=Depends(async_session_maker)):
    async with session() as db:
        room = await db.get(Room, code)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        if len(room.player_ids) >= room.max_players:
            raise HTTPException(status_code=400, detail="Room is full")
        if req.user_id in room.player_ids:
            raise HTTPException(status_code=400, detail="User already in room")
        room.player_ids.append(req.user_id)
        # If enough players, status can move to READY automatically (optional)
        if len(room.player_ids) >= 2:
            room.status = RoomStatus.READY
        await db.commit()
        await db.refresh(room)
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=room.player_ids,
            max_players=room.max_players,
        )

@router.get("/{code}", response_model=RoomResponse)
async def get_room(code: str, session=Depends(async_session_maker)):
    async with session() as db:
        room = await db.get(Room, code)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        return RoomResponse(
            code=room.id,
            host_id=room.host_id,
            status=room.status.value,
            players=room.player_ids,
            max_players=room.max_players,
        )
