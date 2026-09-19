# backend/game/cards.py

"""Definitions for UNO cards.
We model colors, values and the Card dataclass.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional

class Color(str, Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"
    BLUE = "blue"
    WILD = "wild"  # used for wild cards (no color until chosen)

class Value(str, Enum):
    ZERO = "0"
    ONE = "1"
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    FIVE = "5"
    SIX = "6"
    SEVEN = "7"
    EIGHT = "8"
    NINE = "9"
    SKIP = "skip"
    REVERSE = "reverse"
    DRAW_TWO = "draw_two"
    WILD = "wild"
    WILD_DRAW_FOUR = "wild_draw_four"

@dataclass(frozen=True)
class Card:
    color: Color
    value: Value
    # For wild cards the chosen color is stored after a player selects it
    chosen_color: Optional[Color] = None

    def matches(self, other: "Card") -> bool:
        """Return True if this card can be played on top of `other`.
        Rules:
        - Same color
        - Same value
        - Wild cards always match
        """
        if self.color == Color.WILD:
            return True
        if other.color == Color.WILD and other.chosen_color:
            # compare against the color that was chosen for the wildcard on the table
            return self.color == other.chosen_color or self.value == other.value
        return self.color == other.color or self.value == other.value

    def serialize(self) -> dict:
        return {
            "color": self.color.value,
            "value": self.value.value,
            "chosen_color": self.chosen_color.value if self.chosen_color else None,
        }

    @classmethod
    def deserialize(cls, data: dict) -> "Card":
        return cls(
            color=Color(data["color"]),
            value=Value(data["value"]),
            chosen_color=Color(data["chosen_color"]) if data.get("chosen_color") else None,
        )
