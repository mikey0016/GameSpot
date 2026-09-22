"""Yangi UNO qoidalar testi: +2/+4 stack yo'q, draw-then-play, o'rinlar."""
import sys

sys.path.insert(0, "backend")

from game.uno import GameState
from game.cards import Card, Color, Value


def color_cards(color: Color, values) -> list:
    return [Card(color=color, value=v) for v in values]


def test_no_stack_plus4():
    """+4 tashanganda keyingi o'yinchi aynan 4 ta olishi kerak (stack yo'q)."""
    gs = GameState([1, 2])
    # current player 1: +4 tashasin
    p1 = gs._find_player(1)
    plus4 = Card(color=Color.WILD, value=Value.WILD_DRAW_FOUR)
    if plus4 not in p1.hand:
        p1.hand.append(plus4)
    gs.play_card(1, plus4, Color.RED)
    assert gs.pending_draw == 4, f"pending_draw={gs.pending_draw}, 4 bo'lishi kerak"
    # player 2 yechishga urinsa: aynan 4 ta oladi
    before = len(gs._find_player(2).hand)
    gs.draw_cards(2)
    after = len(gs._find_player(2).hand)
    assert after - before == 4, f"{after - before} ta oldi, 4 bo'lishi kerak"
    print("OK: +4 => aynan 4 ta olinadi, stack yo'q")


def test_no_stack_plus2():
    gs = GameState([1, 2])
    top = gs.discard_pile[-1]
    top_color = top.chosen_color if top.color == Color.WILD and top.chosen_color else top.color
    # top rangiga mos +2 (agar top wild bo'lsa RED)
    plus2 = Card(color=top_color if top_color != Color.WILD else Color.RED, value=Value.DRAW_TWO)
    p1 = gs._find_player(1)
    p1.hand.append(plus2)
    gs.play_card(1, plus2)
    assert gs.pending_draw == 2
    # +2 ustiga yana +2 tashlab bo'lmaydi
    another = Card(color=plus2.color, value=Value.DRAW_TWO)
    p2 = gs._find_player(2)
    p2.hand.append(another)
    try:
        gs.play_card(2, another)
        print("XATO: stack taqiqlangan holatda ham ishladi")
        sys.exit(1)
    except ValueError:
        pass
    before = len(p2.hand)
    gs.draw_cards(2)
    assert len(p2.hand) - before == 2
    print("OK: +2 => aynan 2 ta, ustiga stack yo'q")


def test_draw_then_play():
    """Mos karta bo'lmasa 1 ta oladi; olingan karta mosa bo'lsa tashlash mumkin."""
    gs = GameState([1, 2])
    top = gs.discard_pile[-1]
    # player 1 qo'lidagi mos kartalarni olamiz, mos bo'lmagan qilamiz
    p1 = gs._find_player(1)
    p1.hand = [Card(color=Color.GREEN, value=Value.SEVEN)]
    top_color = top.chosen_color if top.color == Color.WILD and top.chosen_color else top.color
    bad = Card(color=Color.GREEN if top_color != Color.GREEN else Color.RED, value=Value.SEVEN)
    p1.hand = [bad]
    before = len(p1.hand)
    gs.draw_cards(1)
    assert len(p1.hand) == before + 1
    if gs.drew_playable:
        # olingan kartani tashlaydi
        drawn = p1.hand[-1]
        gs.play_card(1, Card(color=drawn.color, value=drawn.value))
        assert gs._current_player().user_id != 1
        print("OK: olingan karta tashlandi (draw-then-play)")
    else:
        # navbat avtomatik o'tdi
        assert gs._current_player().user_id != 1
        print("OK: mos karta chiqmadi, navbat o'tdi")


def test_drawn_playable_kept():
    """Invariant: drew_playable == (olingan karta topga mos). Mos bo'lsa navbat qoladi,
    bo'lmasa navbat avtomatik o'tadi."""
    gs = GameState([1, 2])
    top = gs.discard_pile[-1]
    top_color = top.chosen_color if top.color == Color.WILD and top.chosen_color else top.color
    top_color = top_color if top_color != Color.WILD else Color.RED
    p1 = gs._find_player(1)
    p1.hand = []
    for _ in range(20):
        gs.draw_cards(1)
        drawn = p1.hand[-1]
        matches = (drawn.color == Color.WILD or drawn.color == top_color or drawn.value == top.value)
        if matches:
            assert gs.drew_playable is True
            assert gs._current_player().user_id == 1, "mos karta olinsa navbat qolishi kerak"
            print("OK: mos karta olindi -> drew_playable=True, navbat saqlandi")
            return
        else:
            assert gs.drew_playable is False
            assert gs._current_player().user_id != 1, "mos bo'lmasa navbat o'tishi kerak"
            # navbatni qaytaramiz (2 o'yinchili)
            gs.draw_cards(2)
    print("OK: invariant 20 urinishda buzilmadi")


def test_finish_order():
    gs = GameState([1, 2, 3])
    # 1 g'olib bo'lsin
    p1 = gs._find_player(1)
    top = gs.discard_pile[-1]
    top_color = top.chosen_color if top.color == Color.WILD and top.chosen_color else top.color
    win_card = Card(color=top_color if top_color != Color.WILD else Color.RED, value=top.value)
    p1.hand = [win_card]
    gs.play_card(1, win_card)
    assert gs.winner_id == 1
    assert gs.finish_order == [1]
    s = gs.serialize()
    assert s["finish_order"] == [1]
    print("OK: finish_order to'g'ri:", s["finish_order"])


if __name__ == "__main__":
    test_no_stack_plus4()
    test_no_stack_plus2()
    test_draw_then_play()
    test_drawn_playable_kept()
    test_finish_order()
    print("\nBARCHA TESTLAR O'TDI ✅")
