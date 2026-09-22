# backend/api/fun.py
"""Qiziqarli funksiyalar: kunlik bonus, yutuqlar, referal, skin do'koni, turnirlar."""

import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..models.social import OnlineUser, Tournament
from ..database import async_session_maker
from ..tz import now_local
from .social import (
    UserIn,
    GameResultIn,
    _broadcast,
    _broadcast_stats_update,
    _display_name,
    get_session,
)

router = APIRouter(tags=["fun"])


# ---------- Kunlik bonus (streak) ----------

@router.post("/bonus/daily")
async def daily_bonus(req: UserIn, session=Depends(get_session)):
    """Kunlik coin bonus. Ketma-ket kunlar: 1d=25, 2d=50, ... cap 200."""
    async with session() as db:
        u = await db.get(OnlineUser, req.id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        today = now_local().date()
        last = u.daily_last.date() if u.daily_last else None
        if last == today:
            raise HTTPException(400, "Bonus allaqachon olingan — ertaga keling!")
        streak = (u.daily_streak or 0) + 1 if (last and (today - last).days == 1) else 1
        amount = min(25 * streak, 200)
        u.daily_streak = streak
        u.daily_last = now_local()
        u.coins = (u.coins or 0) + amount
        await db.commit()
        await _broadcast_stats_update(db, req.id)
        return {"ok": True, "amount": amount, "streak": streak, "coins": u.coins}


@router.get("/bonus/status/{user_id}")
async def bonus_status(user_id: int, session=Depends(get_session)):
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        if not u:
            return {"available": False, "streak": 0}
        today = now_local().date()
        last = u.daily_last.date() if u.daily_last else None
        return {"available": last != today, "streak": u.daily_streak or 0}


# ---------- Yutuqlar (achievements) ----------

ACHIEVEMENTS = [
    {"id": "first_win", "name": "Birinchi g'alaba", "icon": "🥇", "desc": "Bir marta o'yin yuting"},
    {"id": "win_10", "name": "Serqatnarba", "icon": "🏅", "desc": "10 ta g'alaba"},
    {"id": "win_50", "name": "Legenda", "icon": "🏆", "desc": "50 ta g'alaba"},
    {"id": "games_25", "name": "Doimiy o'yinchi", "icon": "🎮", "desc": "25 ta o'yin o'ynash"},
    {"id": "games_100", "name": "Veteran", "icon": "🎖", "desc": "100 ta o'yin o'ynash"},
    {"id": "streak_7", "name": "Sadoqatli", "icon": "🔥", "desc": "7 kun ketma-ket kirish"},
    {"id": "coins_1000", "name": "Bagamon", "icon": "💰", "desc": "1000 coin to'plash"},
    {"id": "level_10", "name": "Yuksak daraja", "icon": "⚡", "desc": "10-levelga yetish"},
]


@router.get("/achievements/list")
async def achievements_list():
    return {"achievements": ACHIEVEMENTS}


async def _check_achievements(db, u: OnlineUser) -> list:
    """Yutuqlarni tekshir, yangilarini ber. Qaytaradi: yangi badge'lar."""
    have = {a.get("id") for a in (u.achievements or [])}
    won = []
    stats = {
        "first_win": (u.wins or 0) >= 1,
        "win_10": (u.wins or 0) >= 10,
        "win_50": (u.wins or 0) >= 50,
        "games_25": (u.games_played or 0) >= 25,
        "games_100": (u.games_played or 0) >= 100,
        "streak_7": (u.daily_streak or 0) >= 7,
        "coins_1000": (u.coins or 0) >= 1000,
        "level_10": (u.level or 1) >= 10,
    }
    meta = {a["id"]: a for a in ACHIEVEMENTS}
    for aid, ok in stats.items():
        if ok and aid not in have:
            entry = {**meta[aid], "at": now_local().isoformat()}
            u.achievements = (u.achievements or []) + [entry]
            won.append(entry)
    if won:
        await db.commit()
        try:
            await _broadcast({"type": "achievement", "user_id": u.id, "badges": won})
        except Exception:
            pass
    return won


@router.post("/achievements/check")
async def achievements_check(req: UserIn, session=Depends(get_session)):
    async with session() as db:
        u = await db.get(OnlineUser, req.id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        won = await _check_achievements(db, u)
        return {"ok": True, "new": won, "achievements": u.achievements or []}


@router.get("/achievements/{user_id}")
async def achievements_get(user_id: int, session=Depends(get_session)):
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        return {"achievements": (u.achievements if u else []) or []}


# ---------- Referal tizimi ----------

def _gen_ref_code() -> str:
    return ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))


@router.post("/ref/my")
async def ref_my(req: UserIn, session=Depends(get_session)):
    """O'z referal kodim (yo'q bo'lsa yaratiladi) + hisob."""
    async with session() as db:
        u = await db.get(OnlineUser, req.id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if not u.ref_code:
            u.ref_code = _gen_ref_code()
            await db.commit()
        return {"code": u.ref_code, "count": u.ref_count or 0, "reward_per_friend": 100}


@router.post("/ref/apply")
async def ref_apply(req: UserIn, session=Depends(get_session)):
    """Yangi user referal kodni kiritadi: {id: men, nickname: 'KOD'}."""
    code = (req.nickname or "").strip().upper()
    if not code:
        raise HTTPException(400, "Kod kiriting")
    async with session() as db:
        u = await db.get(OnlineUser, req.id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if u.referred_by:
            raise HTTPException(400, "Siz allaqachon referal kiritgansiz")
        inviter = (await db.execute(
            select(OnlineUser).where(OnlineUser.ref_code == code)
        )).scalars().first()
        if not inviter or inviter.id == u.id:
            raise HTTPException(404, "Kod topilmadi")
        u.referred_by = inviter.id
        u.coins = (u.coins or 0) + 50
        inviter.ref_count = (inviter.ref_count or 0) + 1
        inviter.coins = (inviter.coins or 0) + 100
        await db.commit()
        await _broadcast_stats_update(db, inviter.id)
        await _broadcast_stats_update(db, u.id)
        try:
            await _broadcast({"type": "toast", "user_id": inviter.id,
                              "name": _display_name(u), "emoji": "🤝 referal bilan qo'shildi (+100🪙)"})
        except Exception:
            pass
        return {"ok": True, "bonus": 50, "inviter": _display_name(inviter)}


# ---------- Skin do'koni ----------

SKIN_CATALOG = {
    "board": [
        {"id": "classic", "name": "Klassik", "price": 0, "css": ""},
        {"id": "neon", "name": "Neon", "price": 300, "css": "rgba(0,255,255,.08);var(--cyan)"},
        {"id": "gold", "name": "Oltin", "price": 500, "css": "rgba(255,215,0,.10);#ffd700"},
        {"id": "toxic", "name": "Toksik", "price": 400, "css": "rgba(0,255,100,.08);#4dff88"},
    ],
    "frame": [
        {"id": "none", "name": "Oddiy", "price": 0, "css": ""},
        {"id": "gold", "name": "Oltin ramka", "price": 400, "css": "#ffd700"},
        {"id": "fire", "name": "Olov ramka", "price": 600, "css": "#ff6b35"},
        {"id": "rainbow", "name": "Kamalak", "price": 800, "css": "linear-gradient(90deg,#ff4b4b,#ffb648,#51d96b,#4b8bff)"},
    ],
}


@router.get("/shop/catalog")
async def shop_catalog():
    return SKIN_CATALOG


@router.get("/shop/owned/{user_id}")
async def shop_owned(user_id: int, session=Depends(get_session)):
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        return {
            "coins": (u.coins if u else 0),
            "equipped": {"board": (u.skin_board if u else None), "frame": (u.skin_frame if u else None)},
        }


class SkinBuyIn(BaseModel):
    user_id: int
    kind: str      # board | frame
    skin_id: str


@router.post("/shop/buy")
async def shop_buy(req: SkinBuyIn, session=Depends(get_session)):
    items = SKIN_CATALOG.get(req.kind) or []
    skin = next((s for s in items if s["id"] == req.skin_id), None)
    if not skin:
        raise HTTPException(404, "Skin topilmadi")
    async with session() as db:
        u = await db.get(OnlineUser, req.user_id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        price = skin["price"]
        if price > 0 and (u.coins or 0) < price:
            raise HTTPException(400, f"Coin yetmadi (kerak: {price})")
        u.coins = (u.coins or 0) - price
        if req.kind == "board":
            u.skin_board = skin["id"]
        else:
            u.skin_frame = skin["id"]
        await db.commit()
        await _broadcast_stats_update(db, req.user_id)
        return {"ok": True, "coins": u.coins, "skin": skin}


# ---------- Turnir tizimi ----------

class TournamentIn(BaseModel):
    host_id: int
    entry_fee: int = 0


@router.post("/tournaments/create")
async def tournament_create(req: TournamentIn, session=Depends(get_session)):
    fee = max(0, min(int(req.entry_fee or 0), 500))
    async with session() as db:
        while True:
            code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
            existing = (await db.execute(select(Tournament).where(Tournament.code == code))).scalars().first()
            if not existing:
                break
        t = Tournament(code=code, host_id=req.host_id, entry_fee=fee,
                       prize=0, status="open", players=[], matches=[])
        db.add(t)
        await db.commit()
        return {"ok": True, "code": t.code}


@router.post("/tournaments/join/{code}")
async def tournament_join(code: str, req: UserIn, session=Depends(get_session)):
    async with session() as db:
        t = (await db.execute(select(Tournament).where(Tournament.code == code))).scalars().first()
        if not t:
            raise HTTPException(404, "Turnir topilmadi")
        if t.status != "open":
            raise HTTPException(400, "Turnir allaqachon boshlangan")
        u = await db.get(OnlineUser, req.id)
        if not u:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        if any(p.get("id") == req.id for p in (t.players or [])):
            raise HTTPException(400, "Allaqachon qo'shilgansiz")
        if len(t.players or []) >= 8:
            raise HTTPException(400, "Joylar to'lgan (8/8)")
        if t.entry_fee > 0:
            if (u.coins or 0) < t.entry_fee:
                raise HTTPException(400, "Coin yetmadi")
            u.coins -= t.entry_fee
        t.players = (t.players or []) + [{"id": req.id, "name": _display_name(u)}]
        t.prize = (t.prize or 0) + t.entry_fee
        await db.commit()
        await _broadcast({"type": "panel_refresh", "scope": "tournaments"})
        return {"ok": True, "players": len(t.players), "prize": t.prize}


@router.post("/tournaments/start/{code}")
async def tournament_start(code: str, req: UserIn, session=Depends(get_session)):
    """Host boshlaydi: bracket yaratiladi (juft sonli o'yinchilar)."""
    import random
    async with session() as db:
        t = (await db.execute(select(Tournament).where(Tournament.code == code))).scalars().first()
        if not t or t.host_id != req.id:
            raise HTTPException(403, "Faqat turnir egisi boshlaydi")
        if t.status != "open":
            raise HTTPException(400, "Turnir allaqachon boshlangan")
        ps = t.players or []
        if len(ps) < 2:
            raise HTTPException(400, "Kamida 2 o'yinchi kerak")
        random.shuffle(ps)
        ids = [p["id"] for p in ps]
        matches = [{"round": 1, "p1": ids[i], "p2": ids[i + 1], "winner_id": None}
                   for i in range(0, len(ids) - 1, 2)]
        t.matches = matches
        t.status = "running"
        await db.commit()
        await _broadcast({"type": "panel_refresh", "scope": "tournaments"})
        return {"ok": True, "matches": matches}


@router.post("/tournaments/report/{code}")
async def tournament_report(code: str, req: GameResultIn, session=Depends(get_session)):
    """O'yin natijasi turnir bracket'iga yoziladi; final bo'lsa prize beriladi."""
    async with session() as db:
        t = (await db.execute(select(Tournament).where(Tournament.code == code))).scalars().first()
        if not t or t.status != "running":
            raise HTTPException(404, "Turnir topilmadi/yopilgan")
        matches = t.matches or []
        winner_id = (req.players[0].get("id") if req.players else None) or req.winner_id
        for m in matches:
            if m["winner_id"] is None and winner_id in (m["p1"], m["p2"]):
                m["winner_id"] = winner_id
        # Keyingi raund
        cur_round = max((m["round"] for m in matches), default=1)
        winners = [m["winner_id"] for m in matches if m["round"] == cur_round and m["winner_id"] is not None]
        total_cur = len([m for m in matches if m["round"] == cur_round])
        if len(winners) == total_cur and len(winners) > 1:
            for i in range(0, len(winners) - 1, 2):
                matches.append({"round": cur_round + 1, "p1": winners[i], "p2": winners[i + 1], "winner_id": None})
        t.matches = matches
        # Final tugadimi?
        last_round = max((m["round"] for m in matches), default=1)
        last_matches = [m for m in matches if m["round"] == last_round]
        if len(last_matches) == 1 and last_matches[0]["winner_id"] is not None:
            t.status = "finished"
            t.winner_id = last_matches[0]["winner_id"]
            t.finished_at = now_local()
            w = await db.get(OnlineUser, t.winner_id)
            if w:
                w.coins = (w.coins or 0) + (t.prize or 0)
                await _broadcast_stats_update(db, w.id)
                try:
                    await _broadcast({"type": "toast", "user_id": w.id,
                                      "name": _display_name(w), "emoji": f"🏆 turnir g'olibi! +{t.prize}🪙"})
                except Exception:
                    pass
        await db.commit()
        await _broadcast({"type": "panel_refresh", "scope": "tournaments"})
        return {"ok": True, "status": t.status, "winner_id": t.winner_id, "prize": t.prize or 0}


@router.get("/tournaments/open/list")
async def tournaments_open(session=Depends(get_session)):
    async with session() as db:
        rows = (await db.execute(
            select(Tournament).where(Tournament.status == "open").order_by(Tournament.created_at.desc()).limit(20)
        )).scalars().all()
        return {"tournaments": [{
            "code": t.code, "host_id": t.host_id, "entry_fee": t.entry_fee,
            "prize": t.prize or 0, "players": len(t.players or []),
        } for t in rows]}


@router.get("/tournaments/{code}")
async def tournament_get(code: str, session=Depends(get_session)):
    async with session() as db:
        t = (await db.execute(select(Tournament).where(Tournament.code == code))).scalars().first()
        if not t:
            raise HTTPException(404, "Turnir topilmadi")
        return {
            "code": t.code, "status": t.status, "entry_fee": t.entry_fee,
            "prize": t.prize or 0, "players": t.players or [],
            "matches": t.matches or [], "winner_id": t.winner_id, "host_id": t.host_id,
        }
