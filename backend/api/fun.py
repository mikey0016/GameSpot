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

# 🆕 Haqiqiy market: rasmli badge / frame / avatar / banner / background

def _mk(prefix, kind, base, step, urls):
    return [
        {"id": f"{prefix}_{i+1}", "name": f"{prefix.capitalize()} №{i+1}", "price": base + i * step,
         "kind": kind, "url": url}
        for i, url in enumerate(urls)
    ]

BADGE_URLS = [
    'https://images.steamusercontent.com/ugc/1838045577744210991/E51AE9834825D29EA7E1075A63D88FED00FB0813/',
    'https://images.steamusercontent.com/ugc/1838045115026574819/C4ADA3F96D10C8F635CA25AEBD3784A3BF5C9653/',
    'https://images.steamusercontent.com/ugc/1838045115026576667/63A9DBBB6D56E2C861E4BB253433A693F0477BAE/',
    'https://images.steamusercontent.com/ugc/1838045115026576205/B3D5C5D16064A76F0C6F6974B665780E2376A278/',
    'https://images.steamusercontent.com/ugc/1838045115026619365/721BA0C8643867855872E9924BFC9A138AC0C12F/',
    'https://images.steamusercontent.com/ugc/1838045115026695671/3BFA5B38B15C801E3054245FE46DA6A20DEEB8BD/',
    'https://images.steamusercontent.com/ugc/1838045115026696187/0F75EFCC0FE51281CB63764B3067B227C3D63D01/',
    'https://images.steamusercontent.com/ugc/1838045115026697478/968416C9FC451C9B08CC8DDA97F82E02BF824D4C/',
    'https://images.steamusercontent.com/ugc/1838045115026697902/5B8B82B025579C3C13EDFE99A35F7D0C997A6F8F/',
    'https://images.steamusercontent.com/ugc/1838045115027476285/17514EF0D96762A26C2C7D789440AFD6DA7D5FDC/',
    'https://images.steamusercontent.com/ugc/1838045115027475933/3DC67E312625D734727388B773F7B39815D54672/',
    'https://images.steamusercontent.com/ugc/1838045115027477055/FC5C33C1793FB8A2E5E8CE4108027DFBE12C6399/',
    'https://images.steamusercontent.com/ugc/1838045115031329929/AAD3D53B21298F734BD7ED520F565859BB787B90/',
    'https://images.steamusercontent.com/ugc/1838045115035333774/BF6B48C196B9174FFA81EF04CA78EDD69295DEFB/',
    'https://images.steamusercontent.com/ugc/1838045115035324625/EEE040D878E1A12941734B82FBCF2DFD87F1580B/',
    'https://images.steamusercontent.com/ugc/15409034549182281524/EDA6E8FADD5AD1F54A2C4B8B3898000713F53514/',]

FRAME_URLS = [
    'https://mirai.senkuro.net/collectibles/319/d9bc002cdb8a574f23b066755fab404032e2726a.webp',
    'https://mirai.senkuro.net/collectibles/318/5d1514c2ea65ca911101bd6897940dcb4f7219c5.webp',
    'https://mirai.senkuro.net/collectibles/317/1091dcb4497f36acc29720ce9af96202c143fd40.webp',
    'https://mirai.senkuro.net/collectibles/315/a8fd74ca361479cb6f72a4e1df5e2c834c16bb87.webp',
    'https://mirai.senkuro.net/collectibles/314/018f95ac6097b6ef734050e8e6103a50c1f718ea.webp',
    'https://mirai.senkuro.net/collectibles/313/42bf5ce64fbdffd580c0a0d6382c5309b87158e1.webp',
    'https://mirai.senkuro.net/collectibles/312/e1228a806294fca12852ab1f8334e091609ce694.webp',
    'https://mirai.senkuro.net/collectibles/311/9d329c86adb8cbc5eddca7979a7f9c30458ad54a.webp',
    'https://mirai.senkuro.net/collectibles/309/b17466857483412d83655e497b8c5e270dba35bf.webp',
    'https://mirai.senkuro.net/collectibles/270/b5bab97e7f1d9bbfa35056195ad0e4a99ab5b936.webp',
    'https://mirai.senkuro.net/collectibles/263/b9000d3197cbb23c3576c7bc8835d1ea2c0df834.webp',
    'https://mirai.senkuro.net/collectibles/248/7911b5dc6027a89348091a5e22a066310b670a83.webp',
    'https://mirai.senkuro.net/collectibles/167/fc56fa8caf139c3ee0ff76503c7aa8e01221472b.webp',
    'https://mirai.senkuro.net/collectibles/169/e6b2a20184adfd50df75b805428298f4961a4d0f.webp',
    'https://mirai.senkuro.net/collectibles/1/7071cc17e994bbbbdf96021d9515f7d9d4397afc.webp',
    'https://mirai.senkuro.net/collectibles/7/0f38d321e46fed7ef03b73e4099467061aaa3592.webp',
]

AVATAR_URLS = [
    'https://i.pinimg.com/originals/4e/c1/ce/4ec1ce232f690681f559aa5ecc7873d2.jpg',
    'https://i.pinimg.com/originals/f0/da/8d/f0da8dc29e45f95c8af4ac77500582d8.jpg',
    'https://i.pinimg.com/originals/ea/f4/2b/eaf42bb232cecbcd0c369d9f1e62ffd5.jpg',
    'https://i.pinimg.com/originals/58/0c/5c/580c5cdd179539a37064fd5a2e7ea041.jpg',
    'https://i.pinimg.com/originals/46/78/03/467803688ec379bbd322b3c147dc6d9f.jpg',
    'https://i.pinimg.com/originals/36/63/51/36635149599a3b9009422a6090a5a443.jpg',
    'https://i.pinimg.com/originals/b5/df/7b/b5df7b1a1055b68b27c4933630199bd5.jpg',
    'https://i.pinimg.com/originals/4f/8c/b0/4f8cb0b1f8f900094fce0ce68a3cd46d.png',
    'https://i.pinimg.com/1200x/53/b6/9f/53b69f5c50053d3dcc3de2c06d5d6516.jpg',
    'https://i.pinimg.com/originals/9a/98/03/9a980395cff2c355402942f9d0d87db8.jpg',
    'https://i.pinimg.com/originals/29/b7/91/29b79135b1fc62a2457c8eab403851bc.jpg',
    'https://i.pinimg.com/originals/53/83/a0/5383a065ddbf2506da0be617ba4bb0cd.jpg',
    'https://i.pinimg.com/originals/44/0a/b7/440ab7c57587cbda3d290e97958eac62.jpg',
    'https://i.pinimg.com/originals/f0/3d/1d/f03d1d4b9910b969689c4e88f367f4d8.jpg',
    'https://i.pinimg.com/originals/d4/38/1b/d4381b3ea0ebb7f14c0ff33656218b91.jpg',
    'https://i.pinimg.com/originals/73/cd/09/73cd09f43b4ca5b2d56c152a79ac5c60.png',
    'https://i.pinimg.com/originals/e1/8d/e4/e18de4340c6f4aeda2ad8532dcf64a08.jpg',
    'https://i.pinimg.com/1200x/18/35/18/1835183e41beb15e65cbaf78809a99cf.jpg',
    'https://i.pinimg.com/originals/5c/55/fc/5c55fcb4f8f281354c9def9d8727993b.jpg',
    'https://i.pinimg.com/originals/92/23/43/9223430d43fda43bb37acadaa424e767.gif',
    'https://i.pinimg.com/originals/e9/5f/5a/e95f5a0e5d6fa3eb5204838eef6ce7fd.jpg',
    'https://i.pinimg.com/originals/86/de/15/86de153bf00265ff58f0cf66724490bd.jpg',
    'https://i.pinimg.com/originals/1e/07/71/1e077194fcfa49f17803b3fe019ce4b4.jpg',
    'https://i.pinimg.com/originals/0e/5a/d9/0e5ad9c21d975f91c2876015cc44871b.jpg',
]

BANNER_URLS = [
    'https://mirai.senkuro.net/collectibles/328/21ad0615618ad9951419f9e18f61ef6d76f5ede7_320.webm',
    'https://mirai.senkuro.net/collectibles/306/9f9bc6466236c59d04430db2cb33d134a51888aa_320.webm',
    'https://mirai.senkuro.net/collectibles/299/698df52f6cfbcf2a4c1466e23e70298e18b3d301_320.webm',
    'https://mirai.senkuro.net/collectibles/298/0211948ae98a2898fefe88cc9559867f67928737_320.webm',
    'https://mirai.senkuro.net/collectibles/286/ee4994de1e9bb28df1b41bbeb949494f38951fc3.jpeg',
    'https://mirai.senkuro.net/collectibles/295/ee8a1625af1147c899924565dfec3baa67d6625c_320.webm',
    'https://mirai.senkuro.net/collectibles/181/e6a194cbc42b4b7dddc54d6eb39387eed1623785_320.webm',
    'https://mirai.senkuro.net/collectibles/180/450ae1e9a623bbf85005b241f00d5fc3ba9d2317_320.webm',
    'https://mirai.senkuro.net/collectibles/41/778c2a4e122ea7d9af83d1b8dd13e331e96569e6_320.webm',
    'https://mirai.senkuro.net/collectibles/40/51b207ab9ca80077086b3a1eed096f59ca0a52f7_320.webm',
    'https://i.pinimg.com/originals/e1/b4/97/e1b497d5cc627017dcb13fdf3dfe1a3c.jpg',
    'https://i.pinimg.com/originals/16/be/c9/16bec9a832a1b90ca555c363c4837401.jpg',
    'https://i.pinimg.com/originals/17/2f/a8/172fa8dd96d6e13f4b1825594034ec78.jpg',
    'https://i.pinimg.com/originals/f6/f5/cd/f6f5cd63fac0ec3dbf0f73fd74cbae6c.jpg',
    'https://i.pinimg.com/originals/38/6b/0f/386b0f2411fbed04ccc775d8f8498f42.jpg',
    'https://i.pinimg.com/originals/aa/55/41/aa5541d265687d1fb50d15e6088013d6.jpg',
    'https://i.pinimg.com/originals/d2/a6/cc/d2a6cc7134978023c1149b3b27b305d4.gif',
    'https://i.pinimg.com/originals/3a/e6/7d/3ae67df286b9b8e568de17e4657fd21d.jpg',
]

BG_URLS = [
    '/bg/bg_1.jpg', '/bg/bg_2.jpg', '/bg/bg_3.png', '/bg/bg_4.jpg',
    '/bg/bg_5.jpg', '/bg/bg_6.jpg', '/bg/bg_7.jpg', '/bg/bg_8.jpg',
]

MARKET_CATALOG = {
    "badge": _mk("badge", "badge", 100, 10, BADGE_URLS),
    "frame": _mk("ramka", "frame", 200, 20, FRAME_URLS),
    "skin": _mk("avatar", "skin", 150, 15, AVATAR_URLS),
    "banner": _mk("banner", "banner", 300, 40, BANNER_URLS),
    "bg": _mk("oboy", "bg", 300, 25, BG_URLS),
}

# Kiyish mumkin bo'lgan barcha turlar (buy/equip validatsiyasi uchun)
MARKET_KINDS = tuple(MARKET_CATALOG.keys())

# Har bir item'ning "qiymati" (url yoki emoji) -> item (backend render uchun)
_MARKET_BY_ID = {item["id"]: item for items in MARKET_CATALOG.values() for item in items}


def _equipped_market(u: OnlineUser) -> dict:
    """User kiygan kosmetika: {kind: item | None}."""
    return {
        "badge": _MARKET_BY_ID.get(u.skin_badge or ""),
        "frame": _MARKET_BY_ID.get(u.skin_frame or ""),
        "skin": _MARKET_BY_ID.get(u.skin_avatar or ""),
        "banner": _MARKET_BY_ID.get(u.skin_banner or ""),
        "bg": _MARKET_BY_ID.get(u.skin_bg or ""),
    }


@router.get("/shop/catalog")
async def shop_catalog():
    cat = dict(SKIN_CATALOG)
    for kind, items in MARKET_CATALOG.items():
        cat[kind] = items
    return cat


@router.get("/shop/owned/{user_id}")
async def shop_owned(user_id: int, session=Depends(get_session)):
    async with session() as db:
        u = await db.get(OnlineUser, user_id)
        eq = {"board": (u.skin_board if u else None)}
        if u:
            eq.update({
                "badge": u.skin_badge or None,
                "frame": u.skin_frame or None,
                "skin": u.skin_avatar or None,
                "banner": u.skin_banner or None,
                "bg": u.skin_bg or None,
            })
        return {"coins": (u.coins if u else 0), "equipped": eq}


class SkinBuyIn(BaseModel):
    user_id: int
    kind: str      # board | frame | badge | skin | banner | bg
    skin_id: str


@router.post("/shop/buy")
async def shop_buy(req: SkinBuyIn, session=Depends(get_session)):
    # Eski (board/frame-css) va yangi (rasmli market) kataloglar birlashtiriladi
    items = (SKIN_CATALOG.get(req.kind) or []) + (MARKET_CATALOG.get(req.kind) or [])
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
        elif req.kind == "badge":
            u.skin_badge = skin["id"]
        elif req.kind == "skin":
            u.skin_avatar = skin["id"]
        elif req.kind == "banner":
            u.skin_banner = skin["id"]
        elif req.kind == "bg":
            u.skin_bg = skin["id"]
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
