# backend/game/uno.py

"""Core UNO game engine.
Implements game initialization, turn handling, move validation and special card effects.
The engine is pure‑Python and independent of FastAPI – it can be used from WebSocket handlers.

🆕 Custom rules implemented here:
- STACKING MODE: +2/+4 bir jumladan bitta `pending_draw` hisoblagichga qo'shiladi.
  Navbatdagi o'yinchi faqat +2/+4 bilan javob qaytaradi yoki to'liq penaltini oladi.
- SWAP card: tashlanganda o'yinchi tanlagan raqib bilan qo'llarini to'liq almashtiradi.
"""

from __future__ import annotations

import random
from typing import List, Dict, Optional

from .cards import Card, Color, Value, PENALTY_VALUES
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
        # 🆕 Stacking mode: +2 va +4 bitta hisoblagichga yig'iladi (+2=2, +4=+4, +2+4=6...)
        self.pending_draw: int = 0
        self.pending_skip: bool = False
        # Draw-then-play: after drawing a playable card the player may throw it
        self.drew_playable: bool = False
        # 🆕 SWAP: tashlangan SWAP karta indeksi + tanlov holati
        # pending_swap_card: discard tepasidagi SWAP (serializatsiya uchun saqlanadi)
        # pending_swap_by: kim tanlayapti (user_id) — faqat u tanlov qiladi
        # pending_swap: swap amalga oshirilayotgan paytda qulf (race-condition-safe)
        self.pending_swap_card: Optional[Card] = None
        self.pending_swap_by: Optional[int] = None
        self._swap_lock: bool = False
        # 🆕 MULTI-PLACEMENT: o'yinni tark etganlar (disconnect timeout / voluntary leave).
        # Ular finish_order'ga oxirgi o'rinlarda qo'shiladi (chiqish tartibida).
        self.withdrawn: List[int] = []
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
        # Flip top card that is not a Wild Draw Four / Wild / SWAP; if it is a special card, treat its effect as pending
        while True:
            card = self.deck.draw(1)[0]
            if card.value not in {Value.WILD_DRAW_FOUR, Value.WILD, Value.SWAP}:
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
            # Stacking: birinchi karta +2 => birinchi o'yinchi +2 penalti bilan boshlaydi
            self.pending_draw = 2
        # Wild / SWAP cards are never the first card by construction above

    def _current_player(self) -> PlayerState:
        return self.players[self.player_order[self.current_idx]]

    def current_player(self) -> PlayerState:
        """Public accessor used by the WebSocket layer."""
        return self._current_player()

    def _next_index(self) -> int:
        step = 1 if self.direction == Direction.CLOCKWISE else -1
        return (self.current_idx + step) % len(self.player_order)

    def _advance_turn(self) -> None:
        """🆕 Navbatni keyingi ACTIVE playerga o'tkazadi.
        FINISHED (finish_order) va WITHDRAWN (chiqib ketgan) playerlar avtomatik skip qilinadi.
        Infinite loop himoyasi: player_order uzunligidan ko'p qadam qilmaydi."""
        for _ in range(len(self.player_order) + 1):
            self.current_idx = self._next_index()
            uid = self.players[self.player_order[self.current_idx]].user_id
            if uid not in self.finish_order and uid not in self.withdrawn:
                return
        # Barcha playerlar inactive — finalize chaqiriladi (faqat xavfsizlik uchun)

    def active_player_ids(self) -> List[int]:
        """🆕 Hozir o'yinda faol playerlar (tugatmagan va chiqib ketmagan)."""
        return [
            p.user_id for p in self.players
            if p.user_id not in self.finish_order and p.user_id not in self.withdrawn
        ]

    def _maybe_finalize(self) -> None:
        """🆕 MULTI-PLACEMENT finalize: faqat 1 (yoki 0) active player qolganda.
        - Oxirgi active player avtomatik oxirgi placementni oladi
        - Withdrawn playerlar chiqish tartibida undan keyin (yoki oxirida) qo'shiladi
        - Placement ikki marta berilmaydi (idempotent)
        - winner_id = 1st place (rekord/prize mantiqisi o'zgarmaydi)
        """
        if self.winner_id:
            return
        active = self.active_player_ids()
        if len(active) <= 1:
            for uid in active:
                if uid not in self.finish_order:
                    self.finish_order.append(uid)
            for uid in self.withdrawn:
                if uid not in self.finish_order:
                    self.finish_order.append(uid)
            if self.finish_order:
                self.winner_id = self.finish_order[0]
            # SWAP tanlovi qoldi-bo'lsa tozalanadi
            self.pending_swap_card = None
            self.pending_swap_by = None

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

        🆕 STACKING MODE qoidalari (custom):
        - pending_draw > 0 bo'lsa FAQAT +2 yoki +4 tashlanadi (rang-maqang farq qilmaydi)
        - +2 ustiga +2 YOKI +4, +4 ustiga +2 YOKI +4 qo'yiladi (aralash stacking)
        - Oddiy raqam/Skip/Reverse/Wild zanjirni UZOLMAYDI
        """
        player = self._find_player(player_id)
        if card not in player.hand:
            return False

        # --- Stacking (+2/+4 zanjiri) ---
        if self.pending_draw > 0:
            return card.value in PENALTY_VALUES

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
        """Deprecated: kept for compatibility — stacking mode endi can_play ichida."""
        return card.value in PENALTY_VALUES

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

        # 🆕 MULTI-PLACEMENT: hand bo'sh bo'lsa player placement oladi, o'yin DAVOM ETADI.
        # Idempotent: bir player faqat bir marta finish_order'ga qo'shiladi.
        if len(player.hand) == 0:
            if player_id not in self.finish_order:
                self.finish_order.append(player_id)
            # SWAP tanlovi shu playerga tegishli bo'lsa tozalanadi
            if self.pending_swap_by == player_id:
                self.pending_swap_card = None
                self.pending_swap_by = None
        elif len(player.hand) == 1 and not player.called_uno:
            # Penalty – draw two cards automatically
            player.hand.extend(self.deck.draw(2))

        # Reset UNO flag for next turn
        player.called_uno = False

        # 🆕 Faqat 1 (yoki 0) active player qolganda o'yin finalize bo'ladi
        self._maybe_finalize()

        # Advance turn if game not finished (finished playerlarni skip qilib)
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
        # Pending +2/+4: take the whole accumulated penalty (server hisoblaydi —
        # client draw_count yubormaydi), turn ends
        if self.pending_draw > 0:
            player.hand.extend(self.deck.draw(self.pending_draw))
            self.draw_count += self.pending_draw
            self.pending_draw = 0
            self._advance_turn()
            # 🆕 faol player qolmagan bo'lsa finalize
            self._maybe_finalize()
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

    def take_penalty(self, player_id: int) -> Dict:
        """🆕 STACKING RULE 13: player penaltini ixtiyoriy qabul qiladi.
        pending_draw to'liq olinadi, 0 qilinadi va navbat o'tadi.
        Server hisoblaydi — client faqat "TAKE_PENALTY" so'rovini yuboradi.
        """
        if self.winner_id:
            raise ValueError("Game already finished")
        if self._current_player().user_id != player_id:
            raise ValueError("Not this player's turn")
        if self.pending_draw <= 0:
            raise ValueError("No pending penalty")
        player = self._find_player(player_id)
        player.hand.extend(self.deck.draw(self.pending_draw))
        self.draw_count += self.pending_draw
        self.pending_draw = 0
        self.drew_playable = False
        self._advance_turn()
        # 🆕 faol player qolmagan bo'lsa (ikkitalik o'yinda raqib chiqib ketgan bo'lishi mumkin)
        self._maybe_finalize()
        return self.serialize()

    def withdraw_player(self, player_id: int) -> Dict:
        """🆕 RECONNECT SYSTEM: player o'yinni tark etadi (voluntary leave yoki
        5-daqiqalik reconnect window timeout).

        - Idempotent: allaqachon chiqqan player uchun hech narsa qilmaydi
        - Agar o'sha player navbatida bo'lsa navbat keyingi active playerga o'tadi
        - Pending penalti o'sha playerga tegishli bo'lsa tozalanadi (game bloklanmaydi)
        - SWAP tanlovi shu playerga tegishli bo'lsa tozalanadi
        - Oxirgi active player qolganda game avtomatik finalize bo'ladi
        """
        if player_id in self.withdrawn:
            return self.serialize()  # idempotent
        if player_id in self.finish_order:
            return self.serialize()  # allaqachon tugatgan — placement berilgan
        player = self._find_player(player_id)
        was_current = self._current_player().user_id == player_id
        self.withdrawn.append(player_id)
        # Navbatida bo'lsa: pending holatlarni tozalab, navbatni o'tkazamiz
        if was_current:
            if self.pending_draw > 0:
                self.pending_draw = 0
            self.drew_playable = False
            self._advance_turn()
        # SWAP tanlovi shu playerga tegishli bo'lsa tozalanadi
        if self.pending_swap_by == player_id:
            self.pending_swap_card = None
            self.pending_swap_by = None
        self._maybe_finalize()
        return self.serialize()

    def _is_playable_now(self, player_id: int, card: Card) -> bool:
        """Can this card be thrown right now (ignoring hand membership)?
        Stacking faol bo'lsa faqat +2/+4 hisoblanadi."""
        if self.pending_draw > 0:
            return card.value in PENALTY_VALUES
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
    # 🆕 SWAP card helpers
    # ---------------------------------------------------------------------
    def swap_targets(self, player_id: int) -> List[int]:
        """SWAP tanlovi uchun mumkin bo'lgan targetlar.
        🆕 Faqat ACTIVE playerlar (o'zi, tugatganlar va chiqib ketganlar bundan tashqari).
        Faqat SWAP tashlagan o'yinchi so'rasa ishlaydi."""
        if self.pending_swap_by != player_id:
            raise ValueError("SWAP selection not active for this player")
        return [
            p.user_id for p in self.players
            if p.user_id != player_id
            and p.user_id not in self.finish_order
            and p.user_id not in self.withdrawn
        ]

    def swap_players(self, player_id: int, target_id: int) -> Dict:
        """🔄 SWAP effect: player_id va target_id qo'llarini to'liq almashtirish.

        Race-condition-safe: `_swap_lock` bilan bir vaqtda ikki swap bo'lmaydi;
        noto'g'ri callback'lar ValueError bilan rad etiladi.
        - SWAP tashlagan o'yinchi (pending_swap_by) tanlov qila oladi
        - Target o'yin ichidagi faol (g'olib bo'lmagan) o'yinchi bo'lishi shart
        - SWAP kartasi o'zi boshqaga o'tmaydi (discard'da qoladi)
        - Turn order o'zgarmaydi (effect faqat handlarni almashtiradi)
        - UNO flaglari qayta hisoblanadi (hand uzunligi o'zgargani uchun)
        """
        if self.winner_id:
            raise ValueError("Game already finished")
        if self.pending_swap_by != player_id:
            raise ValueError("SWAP selection not active for this player")
        if self._swap_lock:
            raise ValueError("Swap already in progress")
        target = self._find_player(target_id)  # ValueError if not in game
        if target_id == player_id:
            raise ValueError("You cannot swap with yourself")
        if target_id in self.finish_order:
            raise ValueError("Player already finished")
        if target_id in self.withdrawn:
            raise ValueError("Player already left the game")
        self._swap_lock = True
        try:
            a = self._find_player(player_id)
            a_hand = a.hand
            b_hand = target.hand
            a.hand = b_hand
            target.hand = a_hand
            # UNO statuslari yangi hand bo'yicha qayta o'rnatiladi
            a.called_uno = len(a.hand) == 1
            target.called_uno = len(target.hand) == 1
            # SWAP kartasi discard'da qoladi — pending holatni yopamiz
            self.pending_swap_card = None
            self.pending_swap_by = None
        finally:
            self._swap_lock = False
        return self.serialize()

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
            # 🆕 Stacking: hech qachon reset qilmaymiz — faqat QO'SHAMIZ (rule 16)
            self.pending_draw += 2
        elif card.value == Value.WILD_DRAW_FOUR:
            # 🆕 Stacking: +4 ham bir xil hisoblagichga qo'shiladi
            self.pending_draw += 4
        elif card.value == Value.SWAP:
            # 🆕 SWAP: kartani tashlagan o'yinchi target tanlashi kerak.
            # Tanlov WS orqali `swap_players` action'i bilan amalga oshadi.
            self.pending_swap_card = card
            self.pending_swap_by = self._current_player().user_id
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
            # 🆕 SWAP tanlov holati (kim tanlayapti)
            "pending_swap_by": self.pending_swap_by,
            # finish order (1st, 2nd, ...) for the result table
            "finish_order": list(self.finish_order),
            # 🆕 o'yinni tark etganlar (reconnect timeout / voluntary leave)
            "withdrawn": list(self.withdrawn),
            # 🆕 hozir o'yinda faol playerlar (frontend placement/turn UI uchun)
            "active_players": self.active_player_ids(),
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
        obj.pending_draw = data.get("pending_draw", 0)
        return obj

# -------------------------------------------------------------------------
# Utility to generate a short room code (e.g., ABC123)
# -------------------------------------------------------------------------
import string

def generate_room_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))
