# backend/models/game.py

"""SQLAlchemy ORM model for a game instance.
Tracks deck state, current turn, direction, and winner.
"""

from sqlalchemy import Column, String, Integer, BigInteger, JSON, DateTime, Enum, Boolean
from sqlalchemy.orm import declarative_base
import enum
from datetime import datetime

Base = declarative_base()

class GameStatus(str, enum.Enum):
    STARTED = "started"
    FINISHED = "finished"
    PAUSED = "paused"

class Game(Base):
    __tablename__ = "games"

    id = Column(String, primary_key=True, index=True)  # same as room.id for simplicity
    room_id = Column(String, nullable=False, index=True)
    status = Column(Enum(GameStatus), default=GameStatus.STARTED)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Serialized game state – deck, discard pile, player hands, current turn, direction, etc.
    state = Column(JSON, default=dict)
    winner_id = Column(BigInteger, nullable=True)

    def __repr__(self):
        return f"<Game {self.id} status={self.status}>"
