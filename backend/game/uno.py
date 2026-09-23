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
        # Classic rules: +2 forces exactly 2, +4 forces exactly 4 (no stacking)
        self.pending_draw: int = 0
        self.pending_skip: bool = False
        # Draw-then-play: after drawing a playable card the player may throw it
        self.drew_playable: bool = False
        self.winner_id: Optional[int] = None
        # Finish order (1st, 2nd, ...) as players empty their hands
        self.finish_order: List[int] = []
        # 📊 Statistika: davomiylik va faollik hisoblagichlari
        self.started_at = None  # datetime (UTC+5) — main.py o'rnatadi
        self.draw_count: int = 0
        self.turn_count: int = 0
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
            # Classic: first card +2 => first player draws 2 and loses turn
            self.pending_draw = 2
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

        Haqiqiy (house) UNO qoidalari:
        - +2 ustiga faqat +2, +4 ustiga faqat +4 qo'yiladi (stacking) — istalgancha
        - Stack payti boshqa kartalar (jumladan wild) tashlanmaydi
        """
        player = self._find_player(player_id)
        if card not in player.hand:
            return False

        top = self.discard_pile[-1]

        # --- Stacking (+2/+4 zanjiri) ---
        if self.pending_draw > 0:
            # +2 zanjiri: faqat +2 bilan davom etadi
            if top.value == Value.DRAW_TWO:
                return card.value == Value.DRAW_TWO
            # +4 zanjiri: faqat +4 bilan davom etadi
            return card.value == Value.WILD_DRAW_FOUR

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
        """Deprecated: classic rules forbid stacking (kept for compatibility)."""
        return False

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
        self.turn_count += 1

        # Apply card effects
        self._apply_card_effect(card)

        # Draw-then-play window closes after any card is played
        self.drew_playable = False

        # UNO call handling – caller must set player.called_uno before playing second‑to‑last card
        if len(player.hand) == 0:
            self.finish_order.append(player_id)
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
        """Player draws cards; a pending +2/+4 forces the full penalty amount.
        After drawing 1 (no pending penalty), the player MAY play the drawn
        card if it matches (draw-then-play rule).
        """
        if self.winner_id:
            raise ValueError("Game already finished")
        if self._current_player().user_id != player_id:
            raise ValueError("Not this player's turn")
        player = self._find_player(player_id)
        # Pending +2/+4: take the whole accumulated penalty, turn ends
        if self.pending_draw > 0:
            player.hand.extend(self.deck.draw(self.pending_draw))
            self.draw_count += self.pending_draw
            self.pending_draw = 0
            self._advance_turn()
            return self.serialize()
        # Normal draw: exactly one card, then the player may play it if it matches
        player.hand.extend(self.deck.draw(1))
        self.draw_count += 1
        drawn = player.hand[-1]
        self.drew_playable = self._is_playable_now(player_id, drawn)
        # Turn passes only if the drawn card is NOT playable
        if not self.drew_playable:
            self._advance_turn()
        return self.serialize()

    def _is_playable_now(self, player_id: int, card: Card) -> bool:
        """Can this card be thrown right now (ignoring hand membership)?"""
        top = self.discard_pile[-1] if self.discard_pile else None
        if not top:
            return True
        if card.color == Color.WILD:
            return True
        effective_top = Card(
            color=top.chosen_color if top.color == Color.WILD and top.chosen_color else top.color,
            value=top.value,
        )
        return card.color == effective_top.color or card.value == effective_top.value

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
            # Haqiqiy stacking: zanjir qancha uzun bo'lsa, jami shuncha olinadi
            self.pending_draw = (self.pending_draw or 0) + 2
        elif card.value == Value.WILD_DRAW_FOUR:
            self.pending_draw = (self.pending_draw or 0) + 4
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
            # Penalty info so clients can show "take N cards"
            "pending_draw": self.pending_draw,
            # draw-then-play: player may throw the just-drawn card
            "drew_playable": self.drew_playable,
            # finish order (1st, 2nd, ...) for the result table
            "finish_order": list(self.finish_order),
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
