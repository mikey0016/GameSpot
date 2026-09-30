# backend/game/mafia.py

"""Mafia o'yin engine — True Mafia uslubida, WebApp uchun qurilgan.

🆕 YANGI ROLLAR (9 ta):
  DON        — mafia sardori: tuni o'ldirishni tanlaydi, tekshiruvda oddiy mafia kabi ko'rinadi
  MAFIA      — oddiy mafiyachi
  DOCTOR     — tunda bir o'yinchini davolaydi (o'limni bekor qiladi)
  DETECTIVE  — tunda tekshiradi: mafia yoki emas
  WITNESS    — DON'NI tekshirsa HA (Don — mafiya sardori), boshqa mafia'ga YO'Q
  VIGILANTE  — Sheriff: tunda o'z-o'zini o'ldiradi (2 runda tun, 1 runda dam — cycle)
  JUDGE      — Hakam: kunduzi ovoz berishda 2 x ovozi bor (ovozni kuchaytiradi)
  VETERAN    — Eskadron: bir marta tunda o'zini "gvardiya" rejimiga o'tkazadi — tunda uni kimdir o'ldirsa, hujumchi o'ladi
  CIVILIAN   — oddiy fuqaro

G'oliblik shartlari:
  - Mafia soni >= shahar soni  → MAFIA yutadi
  - Barcha mafia o'lgan        → SHAHAR yutadi
  - Maniak yo'q (kelajakda qo'shiladi)

Kod to'liq server-authoritative: barcha harakatlar state ichida validatsiya qilinadi.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime  # noqa: F401
from ..tz import now_local


class MafiaRole:
    DON = "don"
    MAFIA = "mafia"
    DOCTOR = "doctor"
    DETECTIVE = "detective"
    WITNESS = "witness"
    VIGILANTE = "vigilante"
    JUDGE = "judge"
    VETERAN = "veteran"
    CIVILIAN = "civilian"


# Rol ma'lumotlari (frontend'ga yuboriladi — kartalar, emoji, tavsif)
ROLE_INFO = {
    MafiaRole.DON: {
        "name": "Don", "emoji": "🎩", "team": "mafia",
        "color": "#ff4d6d",
        "desc": "Mafiya sardori. Tunlarda o'ldirishni tanlaysiz. Detektiv tekshiruvida oddiy fuqaro kabi ko'rinasiz (Don uchun maxsus WITNESS bor).",
        "night_action": "kill",
    },
    MafiaRole.MAFIA: {
        "name": "Mafiyachi", "emoji": "🔫", "team": "mafia",
        "color": "#ff8fa3",
        "desc": "Don bilan birga tunlarda kimni o'ldirishni tanlaysiz.",
        "night_action": "kill",
    },
    MafiaRole.DOCTOR: {
        "name": "Shifokor", "emoji": "🩺", "team": "city",
        "color": "#4dd4ac",
        "desc": "Har tunda bir kishini davolaysiz. Agar mafiya aynan o'sha kishini o'ldirsa — u tirik qoladi.",
        "night_action": "heal",
    },
    MafiaRole.DETECTIVE: {
        "name": "Detektiv", "emoji": "🕵️", "team": "city",
        "color": "#4da3ff",
        "desc": "Har tunda bir kishini tekshirasiz: MAFIA yoki MUQADDAS ekanini bilib olasiz.",
        "night_action": "investigate",
    },
    MafiaRole.WITNESS: {
        "name": "Guvoh", "emoji": "👁", "team": "city",
        "color": "#b478ff",
        "desc": "Faqat Don'ni tekshirishi mumkin bo'lgan maxsus rol: Don'ni tekshirsangiz HA, boshqa mafiyachi bo'lsa YO'Q.",
        "night_action": "witness",
    },
    MafiaRole.VIGILANTE: {
        "name": "Sheriff", "emoji": "⭐", "team": "city",
        "color": "#ffb347",
        "desc": "Tunda bir kishini o'z-o'zidan o'ldirasiz, lekin har 2 tundan keyin 1 tun dam olasiz.",
        "night_action": "vigilante_kill",
    },
    MafiaRole.JUDGE: {
        "name": "Hakam", "emoji": "⚖️", "team": "city",
        "color": "#ffd700",
        "desc": "Kunduzi ovoz berishda sizning ovozingiz 2 x hisoblanadi.",
        "night_action": None,
    },
    MafiaRole.VETERAN: {
        "name": "Eskadron", "emoji": "🛡️", "team": "city",
        "color": "#8ecae6",
        "desc": "Bir marta tunda «Gvardiya rejimi»ga o'tasiz — o'sha tun sizga hujum qilsa, hujumchi halok bo'ladi.",
        "night_action": "guard",
    },
    MafiaRole.CIVILIAN: {
        "name": "Fuqaro", "emoji": "🧍", "team": "city",
        "color": "#9aa4b2",
        "desc": "Maxsus qobiliyati yo'q, faqat kunduzi ovoz berishda qatnashasiz.",
        "night_action": None,
    },
}


@dataclass
class MafiaPlayer:
    user_id: int
    name: str = "O'yinchi"
    role: str | None = None
    alive: bool = True
    # tun harakati (kim kimga nima qildi)
    night_target: int | None = None
    # vigilante dam olish cycle: 2 tun ish, 1 tun dam
    vigilante_rest: bool = False
    vigilante_work_streak: int = 0
    # guard faollashgan tun (veteran ability)
    guarding: bool = False
    guard_used: bool = False
    # hakam 2x ovoz
    is_judge: bool = False
    # ovoz berish
    vote_target: int | None = None
    # o'yin oxirida rol oshkor qilish
    died_at_phase: str | None = None


@dataclass
class MafiaGameState:
    player_ids: list[int]
    seed: int | None = None

    players: list[MafiaPlayer] = field(default_factory=list)
    phase: str = "lobby"          # lobby | night | day | voting | trial | ended
    day_number: int = 0           # nechanchi kun/tun
    winner_team: str | None = None  # mafia | city
    started_at: datetime | None = None

    # tungi harakatlar logi (har bir o'yinchiga faqat o'zi ko'radi)
    night_actions: dict = field(default_factory=dict)   # actor_id -> {target_id, role}
    night_results: list = field(default_factory=list)   # [str]
    # kunduzgi ovozlar
    votes: dict = field(default_factory=dict)           # voter_id -> target_id
    trial_defendant: int | None = None                  # sudga tushgan (eng ko'p ovoz)
    trial_votes: dict = field(default_factory=dict)     # juror_id -> "guilty"|"innocent"

    # tun natijalari (frontend ko'rsatadi)
    last_night_deaths: list = field(default_factory=list)
    pending_kill: int | None = None                     # doctor tomonidan davolanganligi tekshiriladi
    heal_target: int | None = None
    veteran_guard_active: bool = False

    # chat log (rollar o'ynashi bilan server tomonidan yoziladi)
    log: list = field(default_factory=list)             # [str, ...]

    def __post_init__(self):
        if not self.players:
            self.players = [MafiaPlayer(user_id=pid) for pid in self.player_ids]
        if self.seed is not None:
            random.seed(self.seed)

    # ---------------------------------------------------------------
    # YORDAMCHI FUNKSIYALAR
    # ---------------------------------------------------------------
    def _find(self, uid: int) -> MafiaPlayer | None:
        return next((p for p in self.players if p.user_id == uid), None)

    def alive_players(self) -> list[MafiaPlayer]:
        return [p for p in self.players if p.alive]

    def alive_mafia(self) -> list[MafiaPlayer]:
        return [p for p in self.alive_players() if self._is_mafia_team(p.role)]

    def alive_city(self) -> list[MafiaPlayer]:
        return [p for p in self.alive_players() if not self._is_mafia_team(p.role)]

    @staticmethod
    def _is_mafia_team(role: str | None) -> bool:
        return role in (MafiaRole.DON, MafiaRole.MAFIA)

    def is_mafia(self, uid: int) -> bool:
        p = self._find(uid)
        return bool(p and self._is_mafia_team(p.role))

    def active_player_ids(self) -> list[int]:
        return [p.user_id for p in self.players]

    def add_log(self, text: str):
        self.log.append(text)

    # ---------------------------------------------------------------
    # ROL TAQSIMLASH
    # ---------------------------------------------------------------
    def assign_roles(self) -> None:
        """O'yinchilar soniga mos rollar taqsimlanadi."""
        n = len(self.players)
        if n < 4:
            raise ValueError("Kamida 4 o'yinchi kerak")

        # Mafia soni: 4-5 → 1, 6-8 → 2, 9-11 → 3, 12 → 3
        if n <= 5:
            mafia_count = 1
        elif n <= 8:
            mafia_count = 2
        else:
            mafia_count = 3

        roles: list[str] = [MafiaRole.DON] + [MafiaRole.MAFIA] * (mafia_count - 1)

        # Shifokor, detektiv doim bor
        roles += [MafiaRole.DOCTOR, MafiaRole.DETECTIVE]

        # Qolgan maxsus rollar: shahar a'zolari soniga qarab (doc+det dan keyin), max 4
        specials_pool = [MafiaRole.WITNESS, MafiaRole.VIGILANTE,
                         MafiaRole.JUDGE, MafiaRole.VETERAN]
        random.shuffle(specials_pool)
        # shahar slotlari = n - mafia; ulardan 2 tasi doc+det, qolganlari maxsus/fuqaro
        city_slots = n - mafia_count - 2
        add_specials = max(0, min(city_slots, len(specials_pool)))
        roles += specials_pool[:add_specials]
        # qolganlari fuqaro
        roles += [MafiaRole.CIVILIAN] * (n - len(roles))

        random.shuffle(roles)
        for p, role in zip(self.players, roles):
            p.role = role
            p.alive = True
            p.is_judge = (role == MafiaRole.JUDGE)
            p.night_target = None
            p.vote_target = None
            p.guarding = False
            p.guard_used = False
            p.vigilante_rest = False
            p.vigilante_work_streak = 0
            p.died_at_phase = None

        self.phase = "night"
        self.day_number = 0
        self.started_at = now_local()
        self.add_log("🌃 O'yin boshlandi — rollar tarqatildi!")

    # ---------------------------------------------------------------
    # TUN BOSQICHI
    # ---------------------------------------------------------------
    def start_night(self) -> None:
        """Yangi tun: ovozlar tozalanadi, tun harakatlari qayta boshlanadi."""
        self.phase = "night"
        self.day_number += 1
        self.night_actions = {}
        self.votes = {}
        self.trial_defendant = None
        self.trial_votes = {}
        self.last_night_deaths = []
        self.pending_kill = None
        self.heal_target = None
        self.veteran_guard_active = False
        for p in self.players:
            p.night_target = None
            p.guarding = False
            # ⭐ Sheriff cycle: 2 tun ishlaydi, 1 tun dam oladi (ROLE_INFO'dagi "har 2 tundan keyin 1 tun dam")
            if p.role == MafiaRole.VIGILANTE and p.alive:
                if p.vigilante_rest:
                    p.vigilante_rest = False
                else:
                    p.vigilante_work_streak = (p.vigilante_work_streak or 0) + 1
                    if p.vigilante_work_streak >= 2:
                        p.vigilante_rest = True
                        p.vigilante_work_streak = 0
        self.add_log(f"🌙 {self.day_number}-TUN boshlandi — shahar uyquga ketdi...")

    def can_night_act(self, uid: int) -> bool:
        p = self._find(uid)
        if not p or not p.alive or self.phase != "night":
            return False
        info = ROLE_INFO.get(p.role)
        return bool(info and info.get("night_action"))

    def night_action_role(self, uid: int) -> str | None:
        p = self._find(uid)
        if not p:
            return None
        return ROLE_INFO.get(p.role, {}).get("night_action")

    def set_night_target(self, actor_id: int, target_id: int | None) -> None:
        """Tun harakatini qayd etadi. target_id=None — harakat qilmaslik (o'tkazib yuborish)."""
        p = self._find(actor_id)
        if not p or not p.alive:
            raise ValueError("Siz o'yinda emassiz")
        if self.phase != "night":
            raise ValueError("Hozir tun emas")
        if not self.can_night_act(actor_id):
            raise ValueError("Sizning rolingiz tunda harakat qilmaydi")

        action = self.night_action_role(actor_id)
        if target_id is not None:
            t = self._find(target_id)
            if not t or not t.alive:
                raise ValueError("Nishon topilmadi yoki o'lgan")

        # ⭐ Sheriff dam olish tunda hujum yo'q (lekin skip — target_id=None — mumkin)
        if action == "vigilante_kill" and p.vigilante_rest and target_id is not None:
            raise ValueError("Sheriff dam olyapti — bu tunda hujum qila olmaysiz")
        # 🛡 Veteran: target=o'zi → guard rejimi; boshqani tanlasa — skip (harakat yo'q)
        if action == "guard" and target_id is not None:
            if target_id != actor_id:
                target_id = None  # guard faqat o'zini himoya qiladi — boshqa tanlov skip hisoblanadi
            elif p.guard_used:
                raise ValueError("«Gvardiya rejimi» allaqachon ishlatilgan")

        self.night_actions[actor_id] = {"actor_id": actor_id, "target_id": target_id, "role": p.role}

    def all_night_actions_done(self) -> bool:
        """Barcha tirik faol rollar o'z harakatini qilganmi?"""
        for p in self.alive_players():
            if not self.can_night_act(p.user_id):
                continue
            if p.role == MafiaRole.VIGILANTE and p.vigilante_rest:
                continue  # dam olyapti — avtomatik skip
            if p.user_id not in self.night_actions:
                return False
        return True

    # 🆕 Mafia bir nechta kishi bo'lsa: hamma mafia Don bilan bir ma'noda o'ldirishni tanlaydi
    def mafia_consensus_target(self) -> int | None:
        """Mafia a'zolarining tanlovlaridan konsensus: birinchi qilingan tanlov."""
        for p in self.alive_mafia():
            act = self.night_actions.get(p.user_id)
            if act and act.get("target_id") is not None:
                return act["target_id"]
        return None

    def resolve_night(self) -> list[dict]:
        """Tun yakunlanadi — kim o'ldi, kim tirik qoldi.
        events: [{type: death|save|info, user_id, name, role?, text}] — frontend animatsiya uchun."""
        if self.phase != "night":
            raise ValueError("Hozir tun emas")

        events: list[dict] = []
        deaths: list[int] = []
        checked_info: dict[int, dict] = {}   # actor_id -> natija (faqat shaxsiy)

        # 1) DON/MAFIA o'ldirish
        kill_target = self.mafia_consensus_target()
        if kill_target is not None:
            self.pending_kill = kill_target

        # 2) SHIFOKOR davolash
        heal_act = next((a for a in self.night_actions.values()
                         if a["role"] == MafiaRole.DOCTOR and a["target_id"] is not None), None)
        self.heal_target = heal_act["target_id"] if heal_act else None

        # 3) VETERAN guard
        guard_act = next((a for a in self.night_actions.values()
                          if a["role"] == MafiaRole.VETERAN and a["target_id"] is not None), None)
        if guard_act:
            vp = self._find(guard_act["actor_id"])
            if vp:
                vp.guard_used = True
                vp.guarding = True
                self.veteran_guard_active = True

        # 4) O'limni qayta ishlash
        if self.pending_kill is not None:
            kp = self._find(self.pending_kill)
            if kp and kp.alive:
                # Shifokor davolaganmi?
                if self.heal_target == kp.user_id:
                    events.append({
                        "type": "save", "user_id": kp.user_id, "name": kp.name,
                        "text": f"🩺 {kp.name} ga hujum qilindi, lekin shifokor davolab qo'ydi!",
                    })
                    self.add_log(f"🩺 Shifokor bir jonni o'lim o'tidan qutqardi!")
                # Veteran guard faol va hujum qilingan = veteran o'zi bo'lsa hujumchini o'ldir
                elif self.veteran_guard_active and kp.role == MafiaRole.VETERAN:
                    # hujumchini aniqlaymiz (mafia) va o'ldiramiz
                    for mp in self.alive_mafia():
                        mp.alive = False
                        mp.died_at_phase = "night"
                        deaths.append(mp.user_id)
                        events.append({
                            "type": "death", "user_id": mp.user_id, "name": mp.name,
                            "role": mp.role,
                            "text": f"🛡️ {mp.name} (Eskadron) hujum qildi, lekin qurbanga tegmadi va halok bo'ldi!",
                        })
                else:
                    kp.alive = False
                    kp.died_at_phase = "night"
                    deaths.append(kp.user_id)
                    events.append({
                        "type": "death", "user_id": kp.user_id, "name": kp.name,
                        "role": kp.role,
                        "text": f"💀 {kp.name} tunda o'ldirildi!",
                    })
                    self.add_log(f"💀 {kp.name} tuni o'ldirildi.")

        # ⭐ Sheriff hujumi
        vig_act = next((a for a in self.night_actions.values()
                        if a["role"] == MafiaRole.VIGILANTE and a["target_id"] is not None), None)
        if vig_act:
            vt = self._find(vig_act["target_id"])
            if vt and vt.alive and vt.user_id not in deaths:
                # shifokor davolagan bo'lsa qutqariladi
                if self.heal_target == vt.user_id:
                    events.append({
                        "type": "save", "user_id": vt.user_id, "name": vt.name,
                        "text": f"🩺 Sheriff hujum qildi, lekin shifokor {vt.name} ni davolab qo'ydi!",
                    })
                elif self.veteran_guard_active and vt.role == MafiaRole.VETERAN:
                    shooter = self._find(vig_act["actor_id"])
                    if shooter:
                        shooter.alive = False
                        shooter.died_at_phase = "night"
                        deaths.append(shooter.user_id)
                        events.append({
                            "type": "death", "user_id": shooter.user_id, "name": shooter.name,
                            "role": shooter.role,
                            "text": f"🛡️ Sheriff {vt.name} ga hujum qildi, lekin u Eskadron edi — Sheriff halok bo'ldi!",
                        })
                else:
                    vt.alive = False
                    vt.died_at_phase = "night"
                    deaths.append(vt.user_id)
                    events.append({
                        "type": "death", "user_id": vt.user_id, "name": vt.name,
                        "role": vt.role,
                        "text": f"⭐ Sheriff tuni {vt.name} ni o'ldirdi!",
                    })
                    self.add_log(f"⭐ Sheriff {vt.name} ni o'ldirdi.")

        self.last_night_deaths = deaths

        # 5) DETECTIVE tekshiruvi (faqat tekshiruvchiga shaxsiy natija)
        for actor_id, act in self.night_actions.items():
            if act["role"] == MafiaRole.DETECTIVE and act["target_id"] is not None:
                t = self._find(act["target_id"])
                if t:
                    is_m = self._is_mafia_team(t.role)
                    # Don tekshiruvda oddiy kabi ko'rinadi (truemafia uslubi) — detektivga MAFIA emas
                    shown = is_m and t.role != MafiaRole.DON
                    checked_info[actor_id] = {
                        "target_id": t.user_id, "target_name": t.name,
                        "result": "mafia" if shown else "innocent",
                        "role_shown": "MAFIA" if shown else "MUQADDAS",
                    }

        # 6) WITNESS: faqat DON'ni tekshirsa HA
        for actor_id, act in self.night_actions.items():
            if act["role"] == MafiaRole.WITNESS and act["target_id"] is not None:
                t = self._find(act["target_id"])
                if t:
                    is_don = t.role == MafiaRole.DON
                    checked_info[actor_id] = {
                        "target_id": t.user_id, "target_name": t.name,
                        "result": "don" if is_don else "innocent",
                        "role_shown": "DON TASDIQLANDI" if is_don else "DON EMAS",
                    }

        self.night_results = [
            {"user_id": aid, **info} for aid, info in checked_info.items()
        ]

        # Tun tugadi — kunga o'tamiz
        self.phase = "day"

        # G'oliblikni tekshirish
        self._check_win()
        return events

    # ---------------------------------------------------------------
    # KUN — ovoz berish
    # ---------------------------------------------------------------
    def cast_vote(self, voter_id: int, target_id: int | None) -> None:
        """Kunduzi ovoz berish. target_id=None — betaraf (vozvoz kechish)."""
        p = self._find(voter_id)
        if not p or not p.alive:
            raise ValueError("Siz ovoz bera olmaysiz")
        if self.phase != "day":
            raise ValueError("Hozir ovoz berish vaqti emas")
        if target_id is not None:
            t = self._find(target_id)
            if not t or not t.alive:
                raise ValueError("Nishon o'lgan yoki topilmadi")
        self.votes[voter_id] = target_id

    def all_votes_in(self) -> bool:
        alive = self.alive_players()
        return all(p.user_id in self.votes for p in alive)

    def resolve_votes(self) -> dict | None:
        """Ovozlar tugadi — eng ko'p ovoz olgan sudga chiqadi.
        Returns: {defendant_id, counts: {user_id: votes}, judge_boost: [user_id], skipped: bool}
        None → hech kim sudga chiqmadi (hamma betaraf)."""
        if self.phase != "day":
            raise ValueError("Hozir ovoz berish emas")
        counts: dict[int, int] = {}
        for target in self.votes.values():
            if target is not None:
                counts[target] = counts.get(target, 0) + 1
        # 🆕 Hakam ovozi 2 x
        boosted = []
        for voter_id, target in self.votes.items():
            p = self._find(voter_id)
            if p and p.is_judge and target is not None:
                counts[target] = counts.get(target, 0) + 1  # qo'shimcha 1 → jami 2
                boosted.append(voter_id)

        if not counts:
            self.phase = "night"
            self.add_log("📵 Hech kim ovoz bermadi — kechasi qayta")
            return {"skipped": True, "counts": {}, "judge_boost": boosted}

        max_v = max(counts.values())
        top = [uid for uid, c in counts.items() if c == max_v]
        if len(top) > 1:
            # teng ovoz — hech kim sudga chiqmaydi
            self.phase = "night"
            self.add_log("⚖️ Teng ovoz — hech kim sudga chiqmadi")
            return {"tie": True, "counts": counts, "judge_boost": boosted}

        self.trial_defendant = top[0]
        self.trial_votes = {}
        self.phase = "trial"
        dp = self._find(self.trial_defendant)
        self.add_log(f"⚖️ {dp.name} sudga chiqdi!")
        return {"defendant_id": self.trial_defendant, "counts": counts,
                "judge_boost": boosted, "defendant_name": dp.name}

    def cast_trial_vote(self, juror_id: int, verdict: str) -> None:
        """Sud ovozi: guilty (aybdor) yoki innocent (aybsiz)."""
        p = self._find(juror_id)
        if not p or not p.alive:
            raise ValueError("Ovoz bera olmaysiz")
        if self.phase != "trial":
            raise ValueError("Hozir sud bo'lmayapti")
        if verdict not in ("guilty", "innocent"):
            raise ValueError("Noto'g'ri ovoz")
        self.trial_votes[juror_id] = verdict

    def all_trial_votes_in(self) -> bool:
        alive = self.alive_players()
        return all(p.user_id in self.trial_votes for p in alive)

    def resolve_trial(self) -> dict:
        """Sud natijasi: guilty ko'pchilik bo'lsa aybdor osiladi."""
        if self.phase != "trial":
            raise ValueError("Hozir sud emas")
        guilty = sum(1 for v in self.trial_votes.values() if v == "guilty")
        innocent = sum(1 for v in self.trial_votes.values() if v == "innocent")
        dp = self._find(self.trial_defendant)
        result = {
            "defendant_id": self.trial_defendant,
            "defendant_name": dp.name if dp else "—",
            "guilty": guilty,
            "innocent": innocent,
            "executed": False,
        }
        if guilty > innocent and dp and dp.alive:
            dp.alive = False
            dp.died_at_phase = "day"
            result["executed"] = True
            result["role"] = dp.role
            self.add_log(f"⚖️ {dp.name} osildi — roli: {ROLE_INFO.get(dp.role, {}).get('name', '?')}")
        else:
            self.add_log(f"🕊️ {dp.name} aybsiz deb topildi.")
        # kunga o'tish (yangi tun)
        self.phase = "night_prep"
        self._check_win()
        return result

    # ---------------------------------------------------------------
    # G'OLIBLIK
    # ---------------------------------------------------------------
    def _check_win(self) -> None:
        mafia = len(self.alive_mafia())
        city = len(self.alive_city())
        if mafia == 0:
            self.winner_team = "city"
            self.phase = "ended"
            self.add_log("🎉 SHAHAR g'olib bo'ldi — barcha mafiyachilar yo'q qilindi!")
        elif mafia >= city:
            self.winner_team = "mafia"
            self.phase = "ended"
            self.add_log("🎉 MAFIYA g'olib bo'ldi — shahar qo'lga olindi!")

    # ---------------------------------------------------------------
    # SERIALIZATION (frontend'ga yuborish uchun — xavfsiz, rollar yashirilgan)
    # ---------------------------------------------------------------
    def public_state(self) -> dict:
        """Hammaga ko'rinadigan umumiy holat (rollarsiz)."""
        return {
            "phase": self.phase,
            "day_number": self.day_number,
            "winner_team": self.winner_team,
            "players": [
                {
                    "user_id": p.user_id,
                    "name": p.name,
                    "alive": p.alive,
                    "died_at_phase": p.died_at_phase,
                    "role": p.role if not p.alive else None,   # o'lgan roli oshkor
                }
                for p in self.players
            ],
            "votes": dict(self.votes) if self.phase == "day" else {},
            "trial_votes": {
                # faqat soni ko'rsatiladi, kim kim ovoz bergan sir
                "guilty": sum(1 for v in self.trial_votes.values() if v == "guilty"),
                "innocent": sum(1 for v in self.trial_votes.values() if v == "innocent"),
            } if self.phase == "trial" else {},
            "trial_defendant": self.trial_defendant,
            "last_night_deaths": self.last_night_deaths,
            "log": self.log[-30:],
        }

    def personal_state(self, user_id: int) -> dict:
        """Faqat shu o'yinchiga ko'rinadigan ma'lumot (roli, tungi natijalar)."""
        p = self._find(user_id)
        base = self.public_state()
        if not p:
            return base

        me = {
            "role": p.role,
            "role_info": ROLE_INFO.get(p.role),
            "alive": p.alive,
            "can_night_act": self.can_night_act(user_id) and p.alive,
            "vigilante_rest": p.role == MafiaRole.VIGILANTE and p.vigilante_rest,
            "guard_used": p.guard_used,
        }

        # Mafia o'z jamoasini ko'radi
        teammates = []
        if p.alive and self._is_mafia_team(p.role):
            teammates = [
                {"user_id": m.user_id, "name": m.name, "role": m.role}
                for m in self.players
                if self._is_mafia_team(m.role) and m.user_id != p.user_id
            ]
        me["teammates"] = teammates

        # Tungi natijalar faqat tekshiruvchiga
        my_result = next((r for r in self.night_results if r["user_id"] == user_id), None)
        me["investigation"] = my_result

        # Tungi harakatim
        my_action = self.night_actions.get(user_id)
        me["night_action_done"] = bool(my_action)
        me["night_target"] = my_action["target_id"] if my_action else None

        # Men ovoz berganmi
        me["voted"] = user_id in self.votes
        me["trial_voted"] = user_id in self.trial_votes

        base["me"] = me
        return base
