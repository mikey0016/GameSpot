# test_mafia_rules.py — Mafia engine to'liq test qamrovi
"""Ishga tushirish: .venv/Scripts/python test_mafia_rules.py"""
import os
os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ.setdefault("BOT_USERNAME", "test_bot")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("WEBAPP_URL", "http://localhost:3000")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_check.db")

import random
from backend.game.mafia import (
    MafiaGameState, MafiaRole, ROLE_INFO,
)

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


def make_game(n=6, seed=42):
    s = MafiaGameState(player_ids=list(range(1, n + 1)), seed=seed)
    s.assign_roles()
    s.start_night()
    return s


def role_of(s, uid):
    p = s._find(uid)
    return p.role if p else None


def skip_all_night(s):
    """Har tirik faol rol harakatsiz o'tadi (None target). Dam tundagi sheriff ham skip."""
    for p in s.alive_players():
        if s.can_night_act(p.user_id):
            if p.role == MafiaRole.VIGILANTE and p.vigilante_rest:
                continue  # dam olyapti — harakat shart emas
            s.set_night_target(p.user_id, None)


def do_night(s, kills=None, heals=None):
    """Tun harakatlarini bajarish: kills={actor: target}, heals={actor: target}."""
    kills = kills or {}
    heals = heals or {}
    for p in s.alive_players():
        if not s.can_night_act(p.user_id):
            continue
        if p.role == MafiaRole.VIGILANTE and p.vigilante_rest:
            continue  # dam olyapti
        if p.user_id in kills:
            s.set_night_target(p.user_id, kills[p.user_id])
        elif p.user_id in heals:
            s.set_night_target(p.user_id, heals[p.user_id])
        else:
            s.set_night_target(p.user_id, None)


def day_vote_all(s, votes=None):
    votes = votes or {}
    for p in s.alive_players():
        s.cast_vote(p.user_id, votes.get(p.user_id))


def trial_all(s, verdict="innocent"):
    for p in s.alive_players():
        s.cast_trial_vote(p.user_id, verdict)


# ==================== ROL TAQSIMLASH ====================
print("== ROL TAQSIMLASH ==")
for n, exp_mafia in [(4, 1), (5, 1), (6, 2), (7, 2), (8, 2), (9, 3), (10, 3), (11, 3), (12, 3)]:
    s = MafiaGameState(player_ids=list(range(1, n + 1)), seed=n * 7 + 1)
    s.assign_roles()
    m = len(s.alive_mafia())
    check(f"n={n}: mafia soni {exp_mafia}", m == exp_mafia)
    check(f"n={n}: don bor", any(p.role == MafiaRole.DON for p in s.players))
    check(f"n={n}: doctor bor", any(p.role == MafiaRole.DOCTOR for p in s.players))
    check(f"n={n}: detective bor", any(p.role == MafiaRole.DETECTIVE for p in s.players))
    check(f"n={n}: jami rol = n", len([p for p in s.players if p.role]) == n)
    check(f"n={n}: phase=night", s.phase == "night")

s = make_game(4)
check("assign_roles: phase night", s.phase == "night")
check("assign_roles: started_at o'rnatildi", s.started_at is not None)

# 4 tadan kam -> xato
try:
    MafiaGameState(player_ids=[1, 2, 3]).assign_roles()
    check("3 o'yinchi: ValueError", False)
except ValueError:
    check("3 o'yinchi: ValueError", True)

# ==================== TUN SIKLI (asosiy) ====================
print("== TUN SIKLI ==")
s = make_game(6, seed=11)
mafia_ids = [p.user_id for p in s.players if s._is_mafia_team(p.role)]
doc = next(p.user_id for p in s.players if p.role == MafiaRole.DOCTOR)

# harakat qilmaguncha all_night_actions_done False
check("harakatsiz: all_done False", not s.all_night_actions_done())
skip_all_night(s)
check("skip: all_done True", s.all_night_actions_done())
ev = s.resolve_night()
check("skip tun: o'lim yo'q", len(s.last_night_deaths) == 0)
check("skip tun: phase=day", s.phase == "day")
check("skip tun: events bo'sh", isinstance(ev, list))

# resolve_night keyin chaqirilsa xato
try:
    s.resolve_night()
    check("resolve_night day'da xato", False)
except ValueError:
    check("resolve_night day'da xato", True)

# ==================== MAFIYA O'LDIRISH + SHIFOKOR ====================
print("== MAFIYA + SHIFOKOR ==")
for seed in range(200):
    s = make_game(4, seed=seed)
    mids = [p.user_id for p in s.players if s._is_mafia_team(p.role)]
    if len(mids) == 1:
        break
mid = mids[0]
doc = next(p.user_id for p in s.players if p.role == MafiaRole.DOCTOR)

# mafia doktorni o'ldiradi, doctor o'zini davolaydi -> save
do_night(s, kills={mid: doc}, heals={doc: doc})
ev = s.resolve_night()
check("doctor o'zini davoladi -> save", any(e["type"] == "save" for e in ev))
check("doctor tirik", s._find(doc).alive)

# yangi tun: doctor boshqani davolaydi, doctor o'ladi
s.start_night()
do_night(s, kills={mid: doc}, heals={doc: next(p.user_id for p in s.alive_players() if p.user_id != doc)})
ev = s.resolve_night()
check("doctor o'ldi", not s._find(doc).alive)
check("death event bor", any(e["type"] == "death" for e in ev))

# ==================== DETECTIVE ====================
print("== DETECTIVE ==")
for seed in range(300):
    s = make_game(6, seed=seed)
    mids = [p.user_id for p in s.players if s._is_mafia_team(p.role)]
    if len(mids) >= 2 and any(p.role == MafiaRole.DETECTIVE for p in s.players):
        break
det = next(p.user_id for p in s.players if p.role == MafiaRole.DETECTIVE)
don = next(p.user_id for p in s.players if p.role == MafiaRole.DON)
plain_mafia = next((p.user_id for p in s.players if p.role == MafiaRole.MAFIA), None)

do_night(s, kills={}, heals={})  # hech kim o'ldirmasin (skip hammasi)
# detektiv oddiy mafiyachini tekshiradi -> mafia
if plain_mafia:
    s.set_night_target(det, plain_mafia)
    ev = s.resolve_night()
    res = next(r for r in s.night_results if r["user_id"] == det)
    check("detektiv oddiy mafiyani topadi", res["result"] == "mafia")

# yangi tun: detektiv DON'ni tekshiradi -> innocent (don yashirinadi)
s.start_night()
skip_all_night(s)
s.set_night_target(det, don)
s.resolve_night()
res = next(r for r in s.night_results if r["user_id"] == det)
check("detektiv DON'ni innocent deb ko'radi", res["result"] == "innocent")

# ==================== WITNESS ====================
print("== WITNESS ==")
for seed in range(400):
    s = make_game(8, seed=seed)
    if any(p.role == MafiaRole.WITNESS for p in s.players):
        break
wit = next((p.user_id for p in s.players if p.role == MafiaRole.WITNESS), None)
if wit:
    don = next(p.user_id for p in s.players if p.role == MafiaRole.DON)
    do_night(s)
    s.set_night_target(wit, don)
    s.resolve_night()
    res = next(r for r in s.night_results if r["user_id"] == wit)
    check("witness DON'ni aniqlaydi", res["result"] == "don")
else:
    print("  (witness bu seedda taqsimlanmadi — skip)")

# ==================== VIGILANTE (sheriff) ====================
print("== VIGILANTE ==")
for seed in range(400):
    s = make_game(8, seed=seed)
    if any(p.role == MafiaRole.VIGILANTE for p in s.players):
        break
vig = next((p.user_id for p in s.players if p.role == MafiaRole.VIGILANTE), None)
if vig:
    # 1-tun: ishlaydi
    check("vigilante 1-tun dam emas", not s._find(vig).vigilante_rest)
    target = next(p.user_id for p in s.alive_players() if p.user_id != vig)
    do_night(s)
    s.set_night_target(vig, target)
    ev = s.resolve_night()
    check("vigilante o'ldirdi", not s._find(target).alive)
    # 2-tun: dam
    s.start_night()
    check("vigilante 2-tun dam", s._find(vig).vigilante_rest)
    try:
        s.set_night_target(vig, target)
        check("dam tunda hujum bloklanadi", False)
    except ValueError:
        check("dam tunda hujum bloklanadi", True)
    skip_all_night(s)
    s.resolve_night()
    # 3-tun: yana ishlaydi
    s.start_night()
    check("vigilante 3-tun ishlaydi", not s._find(vig).vigilante_rest)
else:
    print("  (vigilante bu seedda taqsimlanmadi — skip)")

# ==================== JUDGE (2x ovoz) ====================
print("== JUDGE ==")
for seed in range(400):
    s = make_game(9, seed=seed)
    if any(p.role == MafiaRole.JUDGE for p in s.players):
        break
judge = next((p.user_id for p in s.players if p.role == MafiaRole.JUDGE), None)
if judge:
    # tundan kun'ga o'tamiz (resolve_night day'da ovoz ochadi)
    skip_all_night(s)
    s.resolve_night()
    judge = next(p.user_id for p in s.players if p.role == MafiaRole.JUDGE)
    target = next(p.user_id for p in s.alive_players() if p.user_id != judge)
    day_vote_all(s, {judge: target})
    res = s.resolve_votes()
    counts = res.get("counts", {})
    check("judge ovozi 2x hisoblanadi", counts.get(target) == 2)
    check("judge_boost ro'yxatida", judge in res.get("judge_boost", []))
else:
    print("  (judge taqsimlanmadi — skip)")

# ==================== VETERAN (guard) ====================
print("== VETERAN ==")
for seed in range(500):
    s = make_game(9, seed=seed)
    if any(p.role == MafiaRole.VETERAN for p in s.players) and len([p for p in s.players if s._is_mafia_team(p.role)]) >= 1:
        break
vet = next((p.user_id for p in s.players if p.role == MafiaRole.VETERAN), None)
if vet:
    mids = [p.user_id for p in s.players if s._is_mafia_team(p.role)]
    # veteran boshqani tanlasa — skip hisoblanadi (xato emas, guard faqat o'ziga)
    other = next(p.user_id for p in s.alive_players() if p.user_id != vet)
    s.set_night_target(vet, other)
    act = s.night_actions.get(vet, {})
    check("veteran boshqani tanlasa -> skip", act.get("target_id") is None)
    s.night_actions.pop(vet, None)
    do_night(s, kills={mids[0]: vet}, heals={})
    s.set_night_target(vet, vet)
    ev = s.resolve_night()
    check("veteran tirik qoldi", s._find(vet).alive)
    check("veteran guard_used", s._find(vet).guard_used)
    # guard ishlatilgan — yana ishlatib bo'lmaydi
    s.start_night()
    try:
        s.set_night_target(vet, vet)
        check("guard bir marta", False)
    except ValueError:
        check("guard bir marta", True)
else:
    print("  (veteran taqsimlanmadi — skip)")

# ==================== KUN OVOZLARI ====================
print("== KUN OVOZLARI ==")
s = make_game(6, seed=5)
skip_all_night(s)
s.resolve_night()
check("phase=day", s.phase == "day")

# day'da cast_vote ishlaydi
s.cast_vote(1, 2)
check("ovoz qayd etildi", s.votes.get(1) == 2)
# ovozni o'zgartirish mumkin
s.cast_vote(1, 3)
check("ovoz o'zgaradi", s.votes.get(1) == 3)

# hamma ovoz bermaguncha all_votes_in False
check("all_votes_in False", not s.all_votes_in())
day_vote_all(s)
check("all_votes_in True", s.all_votes_in())

res = s.resolve_votes()
check("resolve_votes nativa dict", isinstance(res, dict))

# ovozlar teng -> tie
s2 = MafiaGameState(player_ids=[1, 2, 3, 4], seed=3)
s2.assign_roles()
s2.phase = "day"
s2.votes = {}
# 1->2, 2->1, 3->betaraf, 4->betaraf
s2.cast_vote(1, 2)
s2.cast_vote(2, 1)
s2.cast_vote(3, None)
s2.cast_vote(4, None)
res2 = s2.resolve_votes()
check("teng ovoz -> tie", res2.get("tie") is True)
check("tie -> yangi tun", s2.phase == "night")

# hamma betaraf -> skipped
s3 = MafiaGameState(player_ids=[1, 2, 3, 4], seed=4)
s3.assign_roles()
s3.phase = "day"
for uid in (1, 2, 3, 4):
    s3.cast_vote(uid, None)
res3 = s3.resolve_votes()
check("hamma betaraf -> skipped", res3.get("skipped") is True)

# ==================== SUD ====================
print("== SUD ==")
s = make_game(6, seed=8)
skip_all_night(s)
s.resolve_night()
target = next(p.user_id for p in s.alive_players() if p.user_id != 1)
day_vote_all(s, {1: target, 2: target, 3: target})
res = s.resolve_votes()
check("sudga chiqdi", res.get("defendant_id") == target)
check("phase=trial", s.phase == "trial")

# sud ovozlari
s.cast_trial_vote(1, "guilty")
check("trial ovoz qayd etildi", s.trial_votes.get(1) == "guilty")
try:
    s.cast_trial_vote(1, "maybe")
    check("noto'g'ri verdict xato", False)
except ValueError:
    check("noto'g'ri verdict xato", True)

trial_all(s, "guilty")
tr = s.resolve_trial()
check("aybdor osildi", tr["executed"] is True)
check("osilgan o'lik", not s._find(target).alive)
check("died_at_phase=day", s._find(target).died_at_phase == "day")
check("sud dan keyin night_prep", s.phase == "night_prep")

# innocent -> osilmaydi
s = make_game(6, seed=9)
skip_all_night(s)
s.resolve_night()
target = next(p.user_id for p in s.alive_players() if p.user_id != 1)
day_vote_all(s, {1: target, 2: target, 3: target})
s.resolve_votes()
trial_all(s, "innocent")
tr = s.resolve_trial()
check("aybsiz -> osilmaydi", tr["executed"] is False)
check("aybsiz tirik", s._find(target).alive)

# noto'g'ri phase'da trial vote xato
try:
    s.cast_trial_vote(1, "guilty")
    check("day'da trial vote xato", False)
except ValueError:
    check("day'da trial vote xato", True)

# ==================== G'OLIBLIK ====================
print("== G'OLIBLIK ==")
# mafia == city -> mafia yutadi
s = MafiaGameState(player_ids=[1, 2, 3, 4], seed=1)
s.assign_roles()
for p in s.players:
    p.role = MafiaRole.CIVILIAN
s.players[0].role = MafiaRole.DON
s.players[1].alive = False
s.players[2].alive = False
s._check_win()
check("mafia >= city -> mafia win", s.winner_team == "mafia" and s.phase == "ended")

# hamma mafia o'lsa -> city
s2 = MafiaGameState(player_ids=[1, 2, 3, 4], seed=2)
s2.assign_roles()
for p in s2.players:
    p.role = MafiaRole.CIVILIAN
s2.players[0].role = MafiaRole.DON
s2.players[0].alive = False
s2._check_win()
check("hamma mafia o'lsa -> city win", s2.winner_team == "city" and s2.phase == "ended")

# 3 tirik, 1 mafia 1 city: mafia >= city -> mafia (assign_roles'siz — engine 3 ta o'yinchiga ruxsat bermaydi)
s3 = MafiaGameState(player_ids=[1, 2, 3, 4], seed=3)
s3.assign_roles()
for p in s3.players:
    p.role = MafiaRole.CIVILIAN
s3.players[0].role = MafiaRole.DON
s3.players[1].alive = False
s3.players[2].alive = False
s3._check_win()
check("1 mafia vs 1 city -> mafia", s3.winner_team == "mafia")

# ==================== SERIALIZATSIYA XAVFSIZLIGI ====================
print("== SERIALIZATSIYA ==")
s = make_game(6, seed=13)
skip_all_night(s)
pub = s.public_state()
check("public: rollar yashirilgan", all(p["role"] is None for p in pub["players"]))
check("public: phase to'g'ri", pub["phase"] == s.phase)
check("public: log bor", isinstance(pub["log"], list))

pers = s.personal_state(1)
check("personal: me bor", "me" in pers)
check("personal: rol ko'rinadi", pers["me"]["role"] is not None)
check("personal: role_info bor", pers["me"]["role_info"] is not None)

# o'lgan o'yinchining roli oshkor (bir o'yinchini o'ldiramiz)
victim = next(p for p in s.alive_players() if p.user_id != 1)
victim.alive = False
victim.role = victim.role  # rol saqlanadi
victim.died_at_phase = "night"
pub2 = s.public_state()
vic_in_pub = next(p for p in pub2["players"] if p["user_id"] == victim.user_id)
check("o'lgan roli oshkor", vic_in_pub["role"] == victim.role)

# mafia jamoadoshlarini ko'radi
mafia_p = next((p.user_id for p in s.players if s._is_mafia_team(p.role)), None)
if mafia_p:
    pm = s.personal_state(mafia_p)
    check("mafia jamoasini ko'radi", isinstance(pm["me"]["teammates"], list))

# ==================== XATO HOLATLARI ====================
print("== XATO HOLATLARI ==")
s = make_game(6, seed=21)
# o'lik o'yinchi harakat qila olmaydi
victim = next(p for p in s.players if s._is_mafia_team(p.role))
victim.alive = False
try:
    s.set_night_target(victim.user_id, 1)
    check("o'lik harakat qila olmaydi", False)
except ValueError:
    check("o'lik harakat qila olmaydi", True)

# day'da tungi harakat xato
s.phase = "day"
try:
    s.set_night_target(1, 2)
    check("day'da tungi harakat xato", False)
except ValueError:
    check("day'da tungi harakat xato", True)

# tunda ovoz berish xato
s.phase = "night"
try:
    s.cast_vote(1, 2)
    check("tunda ovoz xato", False)
except ValueError:
    check("tunda ovoz xato", True)

# o'lgan ovoz bera olmaydi
s.phase = "day"
try:
    s.cast_vote(victim.user_id, 2)
    check("o'lik ovoz bera olmaydi", False)
except ValueError:
    check("o'lik ovoz bera olmaydi", True)

# mavjud bo'lmagan nishon
s.phase = "night"
try:
    s.set_night_target(1, 9999)
    check("mavjud emas nishon xato", False)
except ValueError:
    check("mavjud emas nishon xato", True)

# ==================== TO'LIQ O'YIN SIMULYATSIYASI (botlar) ====================
print("== TO'LIQ O'YIN SIMULYATSIYASI ==")


def simulate_full_game(n, seed):
    """Random bot qarorlari bilan to'liq o'yin o'ynash, crash yo'qligini tekshirish."""
    rng = random.Random(seed)
    s = MafiaGameState(player_ids=list(range(1, n + 1)), seed=seed)
    s.assign_roles()
    s.start_night()
    rounds = 0
    while not s.winner_team and rounds < 100:
        rounds += 1
        if s.phase == "night":
            for p in s.alive_players():
                if s.can_night_act(p.user_id):
                    # dam tundagi sheriff — skip
                    if p.role == MafiaRole.VIGILANTE and p.vigilante_rest:
                        s.set_night_target(p.user_id, None)
                        continue
                    targets = [t.user_id for t in s.alive_players() if t.user_id != p.user_id]
                    t = rng.choice(targets) if targets else None
                    if p.role == MafiaRole.VETERAN and not p.guard_used and rng.random() < 0.3:
                        t = p.user_id
                    s.set_night_target(p.user_id, t)
            s.resolve_night()
        elif s.phase == "day":
            for p in s.alive_players():
                targets = [t.user_id for t in s.alive_players() if t.user_id != p.user_id]
                s.cast_vote(p.user_id, rng.choice(targets) if targets else None)
            s.resolve_votes()
            if s.phase == "trial":
                for p in s.alive_players():
                    s.cast_trial_vote(p.user_id, rng.choice(["guilty", "innocent"]))
                s.resolve_trial()
                if s.phase == "night_prep":
                    s.start_night()
        elif s.phase == "night_prep":
            s.start_night()
        else:
            break
    return s.winner_team is not None, rounds, s


games_ok = 0
total_rounds = 0
for seed in range(30):
    ok, rounds, s = simulate_full_game(rng_n := random.Random(seed).choice([4, 5, 6, 7, 8, 9, 10]), seed)
    if ok:
        games_ok += 1
    total_rounds += rounds
check("30 ta random o'yin: hammasi tugadi (crash yo'q)", games_ok == 30)
print(f"  (o'rtacha round: {total_rounds / 30:.1f})")

# g'olib o'yin oxirida rol taqsimoti to'g'ri
ok, _, s = simulate_full_game(8, 15)
check("oxirgi o'yin: hamma o'yinchida rol bor", all(p.role for p in s.players))
check("oxirgi o'yin: winner_team valid", s.winner_team in ("mafia", "city"))

# ==================== ROLE_INFO TO'LIQLIGI ====================
print("== ROLE_INFO ==")
for r in ["don", "mafia", "doctor", "detective", "witness", "vigilante", "judge", "veteran", "civilian"]:
    info = ROLE_INFO.get(r)
    check(f"ROLE_INFO[{r}]", bool(info and info.get("name") and "team" in info and "emoji" in info))

# ==================== YAKUN ====================
print(f"\nPASS: {passed}, FAIL: {failed}")
import sys
sys.exit(0 if failed == 0 else 1)
