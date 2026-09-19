# backend/game/player.py

"""Player state used by the UNO engine.
Each player has a hand (list of Card) and flags for ready / AFK.
"""

from typing import List
from .cards import Card

class PlayerState:
    def __init__(self, user_id: int, username: str | None = None):
        self.user_id = user_id
        self.username = username
        self.hand: List[Card] = []
        self.ready: bool = False
        self.afk: bool = False
        self.called_uno: bool = False

    def serialize(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "hand": [c.serialize() for c in self.hand],
            "ready": self.ready,
            "afk": self.afk,
            "called_uno": self.called_uno,
        }
