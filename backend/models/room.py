# backend/models/room.py

"""SQLAlchemy ORM model for a game room.
A room can host 2‑4 players and tracks its status.
"""

from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey, Enum, JSON
from sqlalchemy.orm import relationship, declarative_base
import enum
from datetime import datetime

Base = declarative_base()

class RoomStatus(str, enum.Enum):
    WAITING = "waiting"
    READY = "ready"
    IN_GAME = "in_game"
    FINISHED = "finished"

class Room(Base):
    __tablename__ = "rooms"

    id = Column(String, primary_key=True, index=True)  # short code like ABC123
    host_id = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(Enum(RoomStatus), default=RoomStatus.WAITING)
    max_players = Column(Integer, default=4)
    # JSON list of player telegram ids for quick lookup
    player_ids = Column(JSON, default=list)
    # optional game instance reference
    game_id = Column(String, nullable=True, index=True)

    def __repr__(self):
        return f"<Room {self.id} status={self.status}>"
