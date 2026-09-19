# backend/models/user.py

"""SQLAlchemy ORM model for a user.
Stores Telegram identity and game statistics.
"""

from sqlalchemy import Column, Integer, String, BigInteger
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, index=True)  # Telegram user_id
    username = Column(String, nullable=True, index=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)

    # Game stats
    games_played = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    xp = Column(Integer, default=0)
    level = Column(Integer, default=1)

    def __repr__(self):
        return f"<User {self.id} ({self.username})>"
