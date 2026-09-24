# backend/game/deck.py

"""Deck handling for UNO.
Creates a full UNO deck, shuffles it and provides draw functionality.
"""

import random
from typing import List
from .cards import Card, Color, Value

class Deck:
    def __init__(self):
        self.cards: List[Card] = self._create_full_deck()
        self.shuffle()

    def _create_full_deck(self) -> List[Card]:
        cards: List[Card] = []
        # One zero per color
        for color in [Color.RED, Color.YELLOW, Color.GREEN, Color.BLUE]:
            cards.append(Card(color=color, value=Value.ZERO))
        # Two of each 1-9 and action cards per color
        for color in [Color.RED, Color.YELLOW, Color.GREEN, Color.BLUE]:
            for value in [Value.ONE, Value.TWO, Value.THREE, Value.FOUR, Value.FIVE,
                          Value.SIX, Value.SEVEN, Value.EIGHT, Value.NINE,
                          Value.SKIP, Value.REVERSE, Value.DRAW_TWO]:
                cards.append(Card(color=color, value=value))
                cards.append(Card(color=color, value=value))
        # 🔄 SWAP: har rangda bittadan (jami 4 ta) — maxsus karta
        for color in [Color.RED, Color.YELLOW, Color.GREEN, Color.BLUE]:
            cards.append(Card(color=color, value=Value.SWAP))
        # Wild cards (no color)
        for _ in range(4):
            cards.append(Card(color=Color.WILD, value=Value.WILD))
            cards.append(Card(color=Color.WILD, value=Value.WILD_DRAW_FOUR))
        return cards

    def shuffle(self) -> None:
        random.shuffle(self.cards)

    def draw(self, count: int = 1) -> List[Card]:
        drawn = self.cards[:count]
        self.cards = self.cards[count:]
        return drawn

    def remaining(self) -> int:
        return len(self.cards)
