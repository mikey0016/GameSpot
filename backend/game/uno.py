# backend/game/uno.py

"""Core UNO game engine.
Implements game initialization, turn handling, move validation and special card effects.
The engine is pure‑Python and independent of FastAPI – it can be used from WebSocket handlers.
"""

from __future__ import annotations

import random
from typing import List, Dict, Optional

from .cards import Card, Color, Value
from .deck import Deck
from .player import PlayerState

class Direction(str):
    CLOCKWISE = "clockwise"
    COUNTER = "counter"

class GameState:
    def __init__(self, player_ids: List[int]):
        if not (2 <= len(player_ids) <= 4):
            raise ValueError("UNO requires 2‑4 players")
        self.players: List[PlayerState] = [PlayerState(uid) for uid in player_ids]
        self.player_order: List[int] = list(range(len(self.players)))  # indexes into self.players
        self.current_idx: int = 0  # who has the turn now (index in player_order)
        self.direction: Direction = Direction.CLOCKWISE
        self.deck = Deck()
        self.discard_pile: List[Card] = []
        self.pending_draw: int = 0  # for Draw Two / Wild Draw Four (stackable)
        self.pending_draw_value: str = "draw_two"  # value of the stacked card
        self.pending_skip: bool = False
        self.winner_id: Optional[int] = None
        self._deal_initial_hands()
        self._start_discard()

    # ---------------------------------------------------------------------
    # Helper utilities
    # ---------------------------------------------------------------------
    def _deal_initial_hands(self) -> None:
        for player in self.players:
            player.hand.extend(self.deck.draw(7))

    def _start_discard(self) -> None:
        # Flip top card that is not a Wild Draw Four; if it is a special card, treat its effect as pending
        while True:
            card = self.deck.draw(1)[0]
            if card.value not in {Value.WILD_DRAW_FOUR, Value.WILD}:
                self.discard_pile.append(card)
                self._apply_initial_card_effect(card)
                break
            else:
                # Put it back to bottom and keep drawing
                self.deck.cards.append(card)
                self.deck.shuffle()

    def _apply_initial_card_effect(self, card: Card) -> None:
        if card.value == Value.SKIP:
            self.pending_skip = True
        elif card.value == Value.REVERSE:
            self._reverse_direction()
        elif card.value == Value.DRAW_TWO:
            self.pending_draw += 2
            self.pending_draw_value = card.value.value
        # Wild cards are never the first card by construction above

    def _current_player(self) -> PlayerState:
        return self.players[self.player_order[self.current_idx]]

    def current_player(self) -> PlayerState:
        """Public accessor used by the WebSocket layer."""
        return self._current_player()

    def _next_index(self) -> int:
        step = 1 if self.direction == Direction.CLOCKWISE else -1
        return (self.current_idx + step) % len(self.player_order)

    def _advance_turn(self) -> None:
        self.current_idx = self._next_index()

    def _reverse_direction(self) -> None:
        self.direction = (
            Direction.COUNTER if self.direction == Direction.CLOCKWISE else Direction.CLOCKWISE
        )
        # When reversing with 2 players, turn stays with same player (rules)
        if len(self.player_order) == 2:
            self.current_idx = self._next_index()

    # ---------------------------------------------------------------------
    # Public API used by WebSocket handlers
    # ---------------------------------------------------------------------
    def can_play(self, player_id: int, card: Card, chosen_color: Optional[Color] = None) -> bool:
        """Validate whether `card` can be played by `player_id` on the current top of discard pile.
        `chosen_color` is only relevant for Wild cards.
        """
        player = self._find_player(player_id)
        if card not in player.hand:
            return False

        # Stacked +2/+4: next player MUST either stack another +2/+4 or draw
        if self.pending_draw > 0:
            return self._can_stack(card)

        top = self.discard_pile[-1]
        # If top is a wild with chosen color, treat that as its effective color
        effective_top = Card(
            color=top.chosen_color if top.color == Color.WILD and top.chosen_color else top.color,
            value=top.value,
        )
        # Wild cards are always playable
        if card.color == Color.WILD:
            return True
        # Normal matching rules
        return card.color == effective_top.color or card.value == effective_top.value

    def _can_stack(self, card: Card) -> bool:
        """While a +2/+4 stack is pending, only a matching penalty card may be played:
        +2 on +2; +4 on +4 (house rule: any wild_draw_four on wild_draw_four);
        +4 may always be stacked on +2.
        """
        if self.pending_draw_value == Value.DRAW_TWO.value:
            return card.value in {Value.DRAW_TWO, Value.WILD_DRAW_FOUR}
        # pending +4: only another +4 can be stacked
        return card.value == Value.WILD_DRAW_FOUR

    def play_card(
        self,
        player_id: int,
        card: Card,
        chosen_color: Optional[Color] = None,
    ) -> Dict:
        """Execute a move where `player_id` plays `card`.
        Returns a dict describing the new state (to be broadcast).
        Raises ValueError on invalid move.
        """
        if self.winner_id:
            raise ValueError("Game already finished")
        if self._current_player().user_id != player_id:
            raise ValueError("Not this player's turn")
        if not self.can_play(player_id, card, chosen_color):
            raise ValueError("Card cannot be played on current pile")

        player = self._find_player(player_id)
        player.hand.remove(card)
        # Handle wild colour choice
        if card.color == Color.WILD:
            if not chosen_color:
                raise ValueError("Wild card requires colour selection")
            card = Card(color=card.color, value=card.value, chosen_color=chosen_color)
        self.discard_pile.append(card)

        # Remember what kind of penalty card is on top while stacking
        if card.value == Value.DRAW_TWO:
            self.pending_draw_value = Value.DRAW_TWO.value
        elif card.value == Value.WILD_DRAW_FOUR:
            self.pending_draw_value = Value.WILD_DRAW_FOUR.value

        # Apply card effects
        self._apply_card_effect(card)

        # UNO call handling – caller must set player.called_uno before playing second‑to‑last card
        if len(player.hand) == 0:
            self.winner_id = player_id
        elif len(player.hand) == 1 and not player.called_uno:
            # Penalty – draw two cards automatically
            player.hand.extend(self.deck.draw(2))

        # Reset UNO flag for next turn
        player.called_uno = False

        # Advance turn if game not finished
        if not self.winner_id:
            if self.pending_skip:
                # Skip next player
                self._advance_turn()
                self.pending_skip = False
            self._advance_turn()

        return self.serialize()

    def draw_cards(self, player_id: int, count: int = 1) -> Dict:
        """Player draws cards; this also satisfies pending +2/+4 stacks.
        While a stack is pending the current player MUST draw the whole
        accumulated amount (or stack another penalty card instead).
        Returns updated state.
        """
        if self.winner_id:
            raise ValueError("Game already finished")
        if self._current_player().user_id != player_id:
            raise ValueError("Not this player's turn")
        player = self._find_player(player_id)
        # If a +2/+4 stack is pending, the player takes the whole accumulated amount
        draw_amount = self.pending_draw if self.pending_draw > 0 else count
        player.hand.extend(self.deck.draw(draw_amount))
        self.pending_draw = 0
        self.pending_draw_value = "draw_two"
        # After drawing, turn passes to next player
        self._advance_turn()
        return self.serialize()

    def call_uno(self, player_id: int) -> None:
        player = self._find_player(player_id)
        if len(player.hand) == 2:
            player.called_uno = True
        else:
            raise ValueError("UNO can only be called when you have exactly two cards")

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    def _find_player(self, user_id: int) -> PlayerState:
        for p in self.players:
            if p.user_id == user_id:
                return p
        raise ValueError("Player not found in this game")

    def _apply_card_effect(self, card: Card) -> None:
        """Modify game state according to the played card (excluding wild colour choice)."""
        if card.value == Value.SKIP:
            self.pending_skip = True
        elif card.value == Value.REVERSE:
            self._reverse_direction()
        elif card.value == Value.DRAW_TWO:
            self.pending_draw += 2
        elif card.value == Value.WILD_DRAW_FOUR:
            self.pending_draw += 4
        # Wild (no extra effect beyond colour selection)

    # ---------------------------------------------------------------------
    # Serialization – used for sending state over WebSocket
    # ---------------------------------------------------------------------
    def serialize(self) -> Dict:
        return {
            "players": [p.serialize() for p in self.players],
            "current_player_id": self._current_player().user_id,
            "direction": self.direction,
            "top_card": self.discard_pile[-1].serialize() if self.discard_pile else None,
            "deck_remaining": self.deck.remaining(),
            "winner_id": self.winner_id,
            # Stacked +2/+4 info so clients can show "take N cards / stack"
            "pending_draw": self.pending_draw,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "GameState":
        """Rehydrate a GameState from a stored dict (e.g., DB JSON field)."""
        obj = cls(player_ids=[])
        obj.players = [PlayerState(p["user_id"], p.get("username")) for p in data["players"]]
        for p, pdata in zip(obj.players, data["players"]):
            p.hand = [Card.deserialize(c) for c in pdata["hand"]]
            p.ready = pdata.get("ready", False)
            p.afk = pdata.get("afk", False)
            p.called_uno = pdata.get("called_uno", False)
        obj.current_idx = next(
            i for i, p in enumerate(obj.players) if p.user_id == data["current_player_id"]
        )
        obj.direction = Direction(data["direction"])
        obj.discard_pile = [Card.deserialize(c) for c in data.get("discard_pile", [])]
        obj.deck.cards = [Card.deserialize(c) for c in data.get("deck", [])]
        obj.winner_id = data.get("winner_id")
        return obj

# -------------------------------------------------------------------------
# Utility to generate a short room code (e.g., ABC123)
# -------------------------------------------------------------------------
import string

def generate_room_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))
