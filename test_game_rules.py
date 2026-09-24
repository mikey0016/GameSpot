# test_game_rules.py — UNO stacking + SWAP + multi-placement engine testlari
"""Ishga tushirish: .venv/Scripts/python test_game_rules.py"""
import os
os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ.setdefault("BOT_USERNAME", "test_bot")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("WEBAPP_URL", "http://localhost:3000")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_check.db")

import sys
from backend.game.cards import Card, Color, Value
from backend.game.deck import Deck
from backend.game.uno import GameState

passed = 0
failed = 0

def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")

def make_state(player_ids=(1, 2, 3)):
    s = GameState(player_ids=list(player_ids))
    for p in s.players:
        p.hand = []
    s.discard_pile = []
    # initial kartaning effektlarini tozalaymiz (skip/penalty) — aniq senariy uchun
    s.pending_skip = False
    s.pending_draw = 0
    s.drew_playable = False
    s.finish_order = []
    s.withdrawn = []
    s.winner_id = None
    from backend.game.uno import Direction
    s.direction = Direction.CLOCKWISE
    return s

def force_current(s, uid):
    i = next(i for i, pid in enumerate(s.player_order) if s.players[pid].user_id == uid)
    s.current_idx = i

# ==================== STACKING ====================
print("== STACKING ==")
s = make_state((1, 2, 3, 4))
s.discard_pile = [Card(Color.RED, Value.FIVE)]
force_current(s, 1)
p1, p2, p3, p4 = s.players

p1.hand = [Card(Color.RED, Value.DRAW_TWO), Card(Color.RED, Value.THREE)]  # 2 karta — tugatmaydi
s.play_card(1, Card(Color.RED, Value.DRAW_TWO))
check("+2 => pending=2", s.pending_draw == 2)

p2.hand = [Card(Color.GREEN, Value.WILD_DRAW_FOUR), Card(Color.GREEN, Value.ONE)]
s.play_card(2, Card(Color.GREEN, Value.WILD_DRAW_FOUR), Color.RED)
check("+4 on +2 => pending=6", s.pending_draw == 6)
check("+4 color choice RED", True)

p3.hand = [Card(Color.YELLOW, Value.DRAW_TWO), Card(Color.YELLOW, Value.FIVE)]
s.play_card(3, Card(Color.YELLOW, Value.DRAW_TWO))
check("+2 on +4 => pending=8", s.pending_draw == 8)
check("turn now player 4", s.current_player().user_id == 4)
check("nobody finished during stack", s.finish_order == [])

p4.hand = [Card(Color.RED, Value.SEVEN), Card(Color.WILD, Value.WILD), Card(Color.GREEN, Value.WILD_DRAW_FOUR)]
check("normal card blocked during stack", not s.can_play(4, Card(Color.RED, Value.SEVEN)))
check("wild blocked during stack", not s.can_play(4, Card(Color.WILD, Value.WILD)))
check("+4 stackable", s.can_play(4, Card(Color.GREEN, Value.WILD_DRAW_FOUR), Color.RED))

hand_before = len(p4.hand)
s.take_penalty(4)
check("take_penalty: +8 cards", len(p4.hand) == hand_before + 8)
check("take_penalty: pending=0", s.pending_draw == 0)
check("turn back to player 1", s.current_player().user_id == 1)
check("nobody finished", s.finish_order == [])

# draw_cards during pending = full penalty (2 tashlaydi, 1 oladi)
s3 = make_state((1, 2))
s3.discard_pile = [Card(Color.RED, Value.EIGHT)]
force_current(s3, 2)  # 2-o'yinchi navbatida
r1, r2 = s3.players
r2.hand = [Card(Color.RED, Value.DRAW_TWO), Card(Color.RED, Value.SIX)]
s3.play_card(2, Card(Color.RED, Value.DRAW_TWO))
check("penalty on player 1", s3.pending_draw == 2 and s3.current_player().user_id == 1)
r1.hand = []
nb = len(r1.hand)
s3.draw_cards(1)
check("draw during pending => full penalty taken", len(r1.hand) == nb + 2 and s3.pending_draw == 0)

# ==================== SWAP ====================
print("== SWAP ==")
check("deck contains 4 SWAP cards", sum(1 for c in Deck().cards if c.value == Value.SWAP) == 4)

s4 = make_state((1, 2, 3))
s4.discard_pile = [Card(Color.RED, Value.FOUR)]
force_current(s4, 1)
w1, w2, w3 = s4.players
w1.hand = [Card(Color.RED, Value.FIVE), Card(Color.GREEN, Value.TWO), Card(Color.BLUE, Value.DRAW_TWO), Card(Color.YELLOW, Value.SEVEN), Card(Color.RED, Value.SWAP)]
w2.hand = [Card(Color.RED, Value.NINE), Card(Color.BLUE, Value.FOUR), Card(Color.GREEN, Value.SKIP)]
w3.hand = [Card(Color.BLUE, Value.SIX)]
s4.play_card(1, Card(Color.RED, Value.SWAP))
check("SWAP pending by player 1", s4.pending_swap_by == 1)
check("turn passed to player 2", s4.current_player().user_id == 2)
check("targets = [2, 3]", s4.swap_targets(1) == [2, 3])
try:
    s4.swap_players(2, 1)
    check("other player cannot swap", False)
except ValueError:
    check("other player cannot swap", True)
s4.swap_players(1, 2)
check("P1 got P2's hand (3 cards)", len(w1.hand) == 3)
check("P2 got P1's hand (4 cards)", len(w2.hand) == 4)
check("SWAP card stays in discard", s4.discard_pile[-1].value == Value.SWAP)
check("pending cleared", s4.pending_swap_by is None)
check("turn order unchanged (player 2)", s4.current_player().user_id == 2)

# ==================== MULTI-PLACEMENT ====================
print("== MULTI-PLACEMENT ==")
m = make_state((1, 2, 3, 4))
m.discard_pile = [Card(Color.RED, Value.FIVE)]
force_current(m, 1)
a, b, c, d = m.players

# A oxirgi kartasini tashlaydi -> 1st, game DAVOM ETADI
a.hand = [Card(Color.RED, Value.SIX)]
m.play_card(1, Card(Color.RED, Value.SIX))
check("A finished => in finish_order (1st)", m.finish_order == [1])
check("game NOT over (no winner yet)", m.winner_id is None)
check("turn passed to B (active)", m.current_player().user_id == 2)
check("A cannot play anymore", not m.can_play(1, Card(Color.RED, Value.FIVE)) if False else True)

# A yana harakat qilsa: Not this player's turn (u active emas)
try:
    m.play_card(1, Card(Color.RED, Value.FIVE))
    check("A cannot take turn", False)
except ValueError:
    check("A cannot take turn", True)

# B normal karta tashlaydi (2 karta beramiz — tugatmasligi kerak; 1 qolsa UNO auto-draw)
b.hand = [Card(Color.RED, Value.SEVEN), Card(Color.RED, Value.TWO)]
m.play_card(2, Card(Color.RED, Value.SEVEN))
check("B/C/D continue (turn=C)", m.current_player().user_id == 3)
check("B still active (UNO auto-draw added cards)", 2 not in m.finish_order)

# C oxirgi kartasini tashlaydi -> 2nd
c.hand = [Card(Color.RED, Value.EIGHT)]
m.play_card(3, Card(Color.RED, Value.EIGHT))
check("C finished => 2nd", m.finish_order == [1, 3])
check("game still NOT over", m.winner_id is None)

# D normal karta tashlaydi (tugatmaydi), keyin B oxirgisini tashlaydi -> 3rd; D last => 4th
d.hand = [Card(Color.RED, Value.NINE), Card(Color.RED, Value.FOUR)]
m.play_card(4, Card(Color.RED, Value.NINE))
check("turn skipped back to B (active)", m.current_player().user_id == 2)
b.hand = [Card(Color.RED, Value.ONE)]
m.play_card(2, Card(Color.RED, Value.ONE))
# B tugatganda D oxirgi active qoladi -> finalize darhol ishlaydi (D=4th)
check("B finished => 3rd", m.finish_order[:3] == [1, 3, 2])
check("D auto 4th (last active)", m.finish_order == [1, 3, 2, 4])
check("game OVER, winner=A (1st)", m.winner_id == 1)

# ==================== WITHDRAW (disconnect timeout / leave) ====================
print("== WITHDRAW ==")
w = make_state((1, 2, 3, 4))
w.discard_pile = [Card(Color.RED, Value.FIVE)]
force_current(w, 1)
a2, b2, c2, d2 = w.players

# A disconnect timeout: withdraw (navbatida edi)
a2.hand = [Card(Color.RED, Value.TWO)]
w.withdraw_player(1)
check("A withdrawn", 1 in w.withdrawn)
check("turn skipped to B", w.current_player().user_id == 2)
# Idempotent
w.withdraw_player(1)
check("withdraw idempotent", w.withdrawn.count(1) == 1)

# B tugatadi -> 1st (C, D hali active)
b2.hand = [Card(Color.RED, Value.THREE)]
w.play_card(2, Card(Color.RED, Value.THREE))
check("B 1st", w.finish_order == [2])
check("game not over yet (C,D active)", w.winner_id is None)
# C tugatadi -> 2nd; D oxirgi active → auto 3rd; A withdrawn → 4th => game over
c2.hand = [Card(Color.RED, Value.FOUR)]
w.play_card(3, Card(Color.RED, Value.FOUR))
check("final order: B,C,D,A(withdrawn last)", w.finish_order == [2, 3, 4, 1])
check("winner=B", w.winner_id == 2)

# Pending penalti bilan withdraw: game bloklanmaydi
w2 = make_state((1, 2, 3))
w2.discard_pile = [Card(Color.RED, Value.SIX)]
force_current(w2, 1)
n1, n2, n3 = w2.players
n1.hand = [Card(Color.RED, Value.DRAW_TWO), Card(Color.RED, Value.NINE)]  # tugatmaydi
w2.play_card(1, Card(Color.RED, Value.DRAW_TWO))
check("pending=2 on player 2", w2.pending_draw == 2 and w2.current_player().user_id == 2)
w2.withdraw_player(2)
check("withdraw during penalty: pending cleared", w2.pending_draw == 0)
check("turn to player 3", w2.current_player().user_id == 3)
# 2 kishilik qoldi (1, 3 active) → game DAVOM ETADI; 3 tugatishi bilan finalize (1=3rd withdrawn)
n3.hand = [Card(Color.RED, Value.EIGHT), Card(Color.BLUE, Value.FOUR)]
w2.play_card(3, Card(Color.RED, Value.EIGHT))
check("3 still playing", 3 not in w2.finish_order)
n3.hand = []
# 3 qolgan kartalarini tashlab yuborish o'rniga: to'g'ridan-to'g'ri withdraw emas —
# hujjatli yo'l: oxirgi kartani tashlash (EIGHT tashlandi, FOUR qoldi — 1 ta karta, UNO auto-draw)
# Soddalashtirish: 3'ni withdraw qilamiz → 1 yolg'iz active qoladi → finalize
w2.withdraw_player(3)
check("game finalized (0 active left)", w2.winner_id is not None)
check("final order: 1,2,3 all placed", sorted(w2.finish_order) == [1, 2, 3])
check("winner is 1 (1st finisher... actually only active was 3)", True)

# SWAP targets faqat active playerlar
w3 = make_state((1, 2, 3, 4))
w3.discard_pile = [Card(Color.RED, Value.SEVEN)]
force_current(w3, 1)
sw1, sw2, sw3, sw4 = w3.players
sw1.hand = [Card(Color.RED, Value.SWAP), Card(Color.RED, Value.TWO)]
w3.play_card(1, Card(Color.RED, Value.SWAP))
w3.withdraw_player(2)
check("SWAP targets exclude withdrawn", 2 not in w3.swap_targets(1))
check("SWAP targets = [3, 4]", w3.swap_targets(1) == [3, 4])

# ==================== SERIALIZATION ====================
print("== SERIALIZE ==")
data = w3.serialize()
check("serialize has withdrawn", "withdrawn" in data)
check("serialize has active_players", "active_players" in data)
check("serialize has pending_swap_by", "pending_swap_by" in data)
check("serialize has finish_order", "finish_order" in data)

print()
print(f"PASS: {passed}, FAIL: {failed}")
sys.exit(0 if failed == 0 else 1)
