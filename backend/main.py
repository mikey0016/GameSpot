# backend/main.py

"""FastAPI entry point for Game Spot.
It registers REST API routers, WebSocket endpoint and includes CORS configuration.
"""

from datetime import datetime, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .config import settings
from .database import async_session_maker, init_db
from .tz import now_local

from .api import rooms_router, social_router, fun_router  # import routers
from .game.uno import GameState
from .models.room import Room
from .api.social import _broadcast as social_broadcast  # noqa: E402  (owner force-leave notifications)

app = FastAPI(title="Game Spot Backend", version="0.1.0")

# CORS – allow Telegram Web App origin (localhost during dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.WEBAPP_URL, "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Placeholder health check
@app.on_event("startup")
async def on_startup():
    await init_db()
    await ensure_owner()
    await cleanup_stale_rooms()


async def ensure_owner():
    """The Telegram id in ADMIN_ID is always the app owner (creates row if needed)."""
    from .models.social import OnlineUser
    admin_id = settings.ADMIN_ID
    if not admin_id:
        return
    async with async_session_maker() as db:
        u = await db.get(OnlineUser, admin_id)
        if not u:
            u = OnlineUser(id=admin_id, role="main_owner")
            db.add(u)
        u.role = "main_owner"
        await db.commit()


async def cleanup_stale_rooms():
    """Delete rooms abandoned before a restart/crash so no ghost rooms linger.
    A room that is still WAITING and has not been touched for a while is stale.
    """
    from datetime import datetime, timedelta, timedelta
    from sqlalchemy import delete
    from .models.room import Room, RoomStatus

    cutoff = now_local() - timedelta(hours=2)
    async with async_session_maker() as db:
        await db.execute(
            delete(Room).where(
                Room.status == RoomStatus.WAITING,
                Room.created_at < cutoff,
            )
        )
        await db.commit()

# Register API routers
app.include_router(rooms_router)
app.include_router(social_router)
app.include_router(fun_router)


# =====================================================================
# ===== BOT AI (bo'sh o'rinlarni to'ldirish) =====
# =====================================================================

BOT_IDS = {-9001: "🤖 Robo", -9002: "🤖 Byte", -9003: "🤖 Nova", -9004: "🤖 Zeta"}


def _bot_play(state, bot_id: int):
    """Bot uchun oddiy AI: mos kartani topib tashla, bo'lmasa ol.
    🆕 Stacking: qo'lida +2/+4 bo'lsa zanjirni davom ettiradi, bo'lmasa penaltini oladi.
    🆕 SWAP: tashlagach eng kam kartali raqibni tanlaydi (3+ o'yinchi bo'lsa).
    """
    from .game.cards import Card, Color, Value, PENALTY_VALUES
    player = state._find_player(bot_id)
    top = state.discard_pile[-1]
    tc = (top.chosen_color or top.color)
    # Pending penalti: qo'lida +2/+4 bo'lsa stack qiladi, aks holda to'liq oladi
    if state.pending_draw > 0:
        stack_card = next((c for c in player.hand if c.value in PENALTY_VALUES), None)
        if stack_card is not None:
            chosen = Color.RED if stack_card.color == Color.WILD else None
            state.play_card(bot_id, stack_card, chosen)
            return {"moved": True, "played": stack_card.value}
        state.take_penalty(bot_id)
        return {"moved": True, "drew": True}
    # SWAP strategiyasi: raqiblardan eng kam kartalisi undan kamroq bo'lsa SWAP tashlaydi
    if any(c.value == Value.SWAP for c in player.hand) and len(state.players) >= 3:
        others = [p for p in state.players if p.user_id != bot_id and p.user_id not in state.finish_order]
        best = min(others, key=lambda p: len(p.hand))
        if len(best.hand) < len(player.hand) - 1:
            swap_card = next(c for c in player.hand if c.value == Value.SWAP)
            state.play_card(bot_id, swap_card)
            state.swap_players(bot_id, best.user_id)
            return {"moved": True, "played": "swap"}
    card = next((c for c in player.hand if c.color == tc or c.value == top.value), None)
    if card is None and any(c.color == Color.WILD for c in player.hand):
        card = next(c for c in player.hand if c.color == Color.WILD)
    if card is None:
        state.draw_cards(bot_id)
        # Olgan karta mosa bo'lsa tashlashga harakat qilamiz (draw-then-play)
        if state.drew_playable:
            drawn = player.hand[-1]
            try:
                chosen = Color.RED if drawn.color == Color.WILD else None
                state.play_card(bot_id, drawn, chosen)
                return {"moved": True, "played": drawn.value}
            except ValueError:
                state.drew_playable = False
                state._advance_turn()
        return {"moved": True, "drew": True}
    chosen = Color.RED if card.color == Color.WILD else None
    state.play_card(bot_id, card, chosen)
    return {"moved": True, "played": card.value}


async def _maybe_bot_turn(room_code: str, state):
    """Navbat botda bo'lsa 1.2s kutib o'ynatadi (chanzli rekursiya)."""
    import asyncio as _asyncio
    if state.winner_id:
        return
    cur = state._current_player().user_id
    if cur not in BOT_IDS:
        return
    await _asyncio.sleep(1.2)
    if games.get(room_code) is not state or state.winner_id:
        return
    try:
        _bot_play(state, cur)
    except ValueError:
        pass
    await manager.broadcast(room_code, {"type": "game_update"})
    await _send_personalized_state(room_code, state)
    if state.winner_id:
        await _finish_game(room_code, state)
        return
    await _maybe_bot_turn(room_code, state)


# =====================================================================
# ===== BLITZ REJIM (turn timer) =====
# =====================================================================

# room_code -> asyncio.TimerHandle (blitz vaqt tugashi)
_blitz_timers: dict = {}
BLITZ_SECONDS = 10


async def _blitz_timeout(room_code: str, user_id: int):
    """Vaqt tugadi: avtomatik karta oladi (yoki pending penalty)."""
    state = games.get(room_code)
    if not state or state.winner_id:
        return
    if state._current_player().user_id != user_id:
        return
    try:
        if state.pending_draw > 0:
            # 🆕 Stacking: penalti faol bo'lsa to'liq miqdorni oladi (take_penalty)
            state.take_penalty(user_id)
        else:
            state.draw_cards(user_id)
            if state.drew_playable:
                state.drew_playable = False
                state._advance_turn()
    except ValueError:
        pass
    _blitz_timers.pop(room_code, None)
    await manager.broadcast(room_code, {
        "type": "blitz_timeout", "user_id": user_id,
        "message": "⏱ Vaqt tugadi — karta avtomatik olindi",
    })
    await manager.broadcast(room_code, {"type": "game_update"})
    await _send_personalized_state(room_code, state)


def _blitz_arm(room_code: str, state):
    """Blitz timer'ni navbatdagi userga o'rnatadi (agar blitz rejim bo'lsa)."""
    import asyncio as _asyncio
    if room_code in _blitz_timers:
        _blitz_timers[room_code].cancel()
        _blitz_timers.pop(room_code, None)
    blitz_on = getattr(state, "blitz", False)
    if not blitz_on or state.winner_id:
        return
    cur = state._current_player().user_id
    if cur in BOT_IDS:
        return  # botlar tez o'ynaydi
    loop = _asyncio.get_event_loop()
    _blitz_timers[room_code] = loop.call_later(
        BLITZ_SECONDS, lambda: _asyncio.ensure_future(_blitz_timeout(room_code, cur))
    )


# Public config for the WebApp (no secrets) – used for building share/invite links
@app.get("/config")
async def get_config():
    return {"bot_username": settings.BOT_USERNAME}

# ------------------- WebSocket manager placeholder -------------------

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, set[WebSocket]] = {}
        # room_code -> {user_id: websocket} for personalized state messages
        self.user_sockets: dict[str, dict[int, WebSocket]] = {}
        # user_id -> last known name for chat/reaction labels
        self.user_names: dict[int, str] = {}
        # personal channel: user_id -> websocket (global WS identifies with {action: hello})
        self.personal_ws: dict[int, WebSocket] = {}

    async def connect(self, room_code: str, websocket: WebSocket, user_id: int | None = None, name: str | None = None):
        await websocket.accept()
        self.active_connections.setdefault(room_code, set()).add(websocket)
        if user_id is not None:
            self.user_sockets.setdefault(room_code, {})[user_id] = websocket
            if name:
                self.user_names[user_id] = name

    def disconnect(self, room_code: str, websocket: WebSocket):
        self.active_connections.get(room_code, set()).discard(websocket)
        if room_code in self.user_sockets:
            self.user_sockets[room_code] = {
                uid: ws for uid, ws in self.user_sockets[room_code].items() if ws is not websocket
            }
            if not self.user_sockets[room_code]:
                self.user_sockets.pop(room_code, None)
        if not self.active_connections.get(room_code):
            self.active_connections.pop(room_code, None)

    async def broadcast(self, room_code: str, message: dict):
        for connection in list(self.active_connections.get(room_code, [])):
            try:
                await connection.send_json(message)
            except Exception:
                pass

    async def send_to_user(self, room_code: str, user_id: int, message: dict):
        """Send a message only to one user's socket in a room."""
        ws = self.user_sockets.get(room_code, {}).get(user_id)
        if ws:
            try:
                await ws.send_json(message)
            except Exception:
                pass

    def register_personal(self, user_id: int, websocket: WebSocket):
        self.personal_ws[user_id] = websocket

    def unregister_personal(self, websocket: WebSocket):
        for uid, ws in list(self.personal_ws.items()):
            if ws is websocket:
                self.personal_ws.pop(uid, None)

    async def leave_all(self, user_id: int, reason: str = ""):
        """Remove a user from every room/game they are inside; returns room count."""
        rooms: list[str] = []
        for code, mapping in list(self.user_sockets.items()):
            if code == "global" or user_id not in mapping:
                continue
            rooms.append(code)
            async with async_session_maker() as db:
                room = await db.get(Room, code)
                if room is not None:
                    # MUHIM: ID lar int saqlanadi (str ga o'tkazilmaydi) — aks holda
                    # player_ids ["123"] bo'lib qoladi va _enrich_players asyncpg
                    # DataError bilan /rooms/open ni 500 ga tushiradi.
                    # Eski string ID lar ham shu yerda int ga normalizatsiya qilinadi.
                    remaining = []
                    for pid in (room.player_ids or []):
                        if str(pid) == str(user_id):
                            continue
                        try:
                            remaining.append(int(pid))
                        except (TypeError, ValueError):
                            continue
                    if str(room.host_id) == str(user_id) or not remaining:
                        await db.delete(room)
                    else:
                        room.player_ids = remaining
                    await db.commit()
            await self.broadcast(code, {
                "type": "player_kicked",
                "user_id": user_id,
                "name": self.user_names.get(user_id, "O'yinchi"),
                "reason": reason,
            })
            await social_broadcast({
                "type": "toast",
                "user_id": user_id,
                "name": self.user_names.get(user_id, "O'yinchi"),
                "emoji": "👢 chiqarildi",
            })
            self.user_sockets.pop(code, None)
            g = games.pop(code, None)
            if g is not None:
                await self.broadcast(code, {"type": "room_closed"})
        return len(rooms)

manager = ConnectionManager()

# NOTE: /ws/global MUST be declared BEFORE /ws/{room_code}, otherwise the
# parameterized route captures "global" as a room code.

# Global chat WebSocket: every WebApp client connects here for worldwide chat
async def _chat_meta(db, user_id) -> dict:
    """Role tag + block/mute info for chat payloads (DB lookup, cheap at this scale)."""
    if user_id is None:
        return {"role": None, "blocked": False, "blocked_until": None, "block_reason": None,
                "muted": False, "muted_until": None}
    from .models.social import OnlineUser
    from datetime import datetime, timedelta as _dt
    u = await db.get(OnlineUser, user_id)
    blocked = bool(u and u.blocked)
    # Muddati chiqqan blokni avtomatik o'chirish
    if blocked and u and u.blocked_until and u.blocked_until <= now_local():
        u.blocked = 0
        u.blocked_until = None
        u.block_reason = None
        await db.commit()
        blocked = False
    muted = bool(u and u.muted)
    # Muddati chiqqan mute'ni avtomatik o'chirish
    if muted and u and u.muted_until and u.muted_until <= now_local():
        u.muted = 0
        u.muted_until = None
        await db.commit()
        muted = False
    # Cosmetics for chat/display everywhere
    cosmetics = None
    if u:
        try:
            from .api.social import _cosmetics_of as _c_of
            cosmetics = _c_of(u)
        except Exception:
            cosmetics = None
    return {
        "role": (u.role if u else None),
        "blocked": blocked,
        "blocked_until": (u.blocked_until.isoformat() if u and u.blocked_until else None),
        "block_reason": (u.block_reason if u else None),
        "muted": muted,
        "muted_until": (u.muted_until.isoformat() if u and u.muted_until else None),
        "cosmetics": cosmetics,
    }


@app.websocket("/ws/global")
async def global_websocket(websocket: WebSocket):
    await manager.connect("global", websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # --- personal channel registration (owner_kick etc.) ---
            if data.get("action") == "hello":
                uid = data.get("user_id")
                if uid is not None:
                    manager.register_personal(uid, websocket)
                continue
            if data.get("action") == "ping":
                continue
            if data.get("action") == "chat":
                text = str(data.get("text", ""))[:300]
                if text.strip():
                    uid = data.get("user_id")
                    saved_msg_id = None  # unique DB id — idempotent frontend render uchun
                    async with async_session_maker() as db:
                        meta = await _chat_meta(db, uid)
                        if not meta["blocked"]:
                            # Persist so history survives reconnects/reloads
                            from .models.social import ChatMessage
                            _msg = ChatMessage(
                                user_id=uid if isinstance(uid, int) else 0,
                                name=data.get("name") or "O'yinchi",
                                text=text.strip(),
                            )
                            db.add(_msg)
                            await db.commit()
                            saved_msg_id = _msg.id  # expire_on_commit=False -> PK saqlanadi
                    if meta["blocked"]:
                        # Bloklangan userga blok ekrani ma'lumotini yuboramiz
                        if uid is not None:
                            await manager.send_to_user("global", uid, {
                                "type": "user_blocked",
                                "user_id": uid,
                                "blocked": True,
                                "reason": meta.get("block_reason"),
                                "until": meta.get("blocked_until"),
                            })
                        continue  # blocked users cannot chat
                    if meta["muted"]:
                        if uid is not None:
                            await manager.send_to_user("global", uid, {
                                "type": "muted",
                                "muted_until": meta.get("muted_until"),
                            })
                        continue  # muted users cannot chat either
                    await manager.broadcast("global", {
                        "type": "chat",
                        "room": "global",
                        "msg_id": saved_msg_id,  # unique: idempotent render (duplikatsiz)
                        "user_id": uid,
                        "name": data.get("name") or "O'yinchi",
                        "role": meta["role"],
                        "cosmetics": meta.get("cosmetics"),
                        "text": text.strip(),
                        "ts": now_local().isoformat(),
                    })
    except WebSocketDisconnect:
        manager.disconnect("global", websocket)
        manager.unregister_personal(websocket)
    except Exception:
        manager.disconnect("global", websocket)
        manager.unregister_personal(websocket)

@app.websocket("/ws/{room_code}")
async def websocket_endpoint(room_code: str, websocket: WebSocket, token: str = ""):
    """Room socket: lobby + real UNO game + chat + reactions.
    Clients send {action: hello, user_id, name} right after connecting.
    """
    user_id: int | None = None
    await manager.connect(room_code, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            # --- keepalive (client keepalive ping; javob shart emas) ---
            if action == "ping":
                continue

            # --- identify the connection ---
            if action == "hello":
                user_id = data.get("user_id")
                name = data.get("name") or "O'yinchi"
                if user_id is not None:
                    manager.user_sockets.setdefault(room_code, {})[user_id] = websocket
                    manager.user_names[user_id] = name
                    await manager.send_to_user(room_code, user_id, {"type": "hello_ok"})
                    # Rejoin: if a game is running, send this player the current state
                    state = games.get(room_code)
                    if state and state.winner_id is None:
                        # 🆕 RECONNECT: agar shu player disconnect timerda bo'lsa — bekor qilamiz
                        _cancel_disconnect_timer(room_code, user_id)
                        await manager.broadcast(room_code, {
                            "type": "player_reconnected", "user_id": user_id,
                            "name": name,
                        })
                        await _send_personalized_state(room_code, state)
                continue

            # --- host starts the game: deal cards, create real UNO state ---
            if action == "start_game" and user_id is not None:
                room = None
                async with async_session_maker() as db:
                    room = await db.get(Room, room_code)
                if not room:
                    await manager.broadcast(room_code, {"type": "room_closed"})
                    continue
                if user_id != room.host_id:
                    continue  # only host may start
                if len(room.player_ids) < 2:
                    await manager.send_to_user(room_code, user_id, {
                        "type": "error", "message": "Kamida 2 o'yinchi kerak"
                    })
                    continue
                # If a game is already running for this room, resume it
                if room_code in games and games[room_code].winner_id is None:
                    await _send_personalized_state(room_code, games[room_code])
                    await manager.broadcast(room_code, {"type": "game_start"})
                    continue
                try:
                    # player_ids dagi eski string ID larni int ga o'tkazamiz
                    ids = []
                    for _p in (room.player_ids or []):
                        try:
                            ids.append(int(_p))
                        except (TypeError, ValueError):
                            continue
                    # 🤖 Bo'sh o'rinlarni bot to'ldirish (host so'ragan bo'lsa yoki 2tadan kam)
                    want_bots = bool(data.get("with_bots")) or data.get("fill_bots")
                    if want_bots and len(ids) < 4:
                        import random as _r
                        bot_ids = _r.sample(sorted(BOT_IDS.keys()), min(4 - len(ids), 4 - len(ids)))
                        for bid in bot_ids:
                            ids.append(bid)
                            manager.user_names.setdefault(bid, BOT_IDS[bid])
                    # ⚡ Blitz rejim
                    blitz = bool(data.get("blitz"))
                except ValueError:
                    continue
                try:
                    state = GameState(player_ids=ids)
                except ValueError:
                    continue
                state.started_at = now_local()
                state.blitz = blitz
                games[room_code] = state
                if blitz:
                    _blitz_arm(room_code, state)
                if any(i in BOT_IDS for i in ids):
                    import asyncio as _a
                    _a.ensure_future(_maybe_bot_turn(room_code, state))
                await manager.broadcast(room_code, {"type": "game_start"})
                await _send_personalized_state(room_code, state)
                continue

            # --- real UNO moves ---
            if action == "play_card" and user_id is not None:
                state = games.get(room_code)
                if not state:
                    continue
                try:
                    from .game.cards import Card, Color, Value
                    card = Card(
                        color=Color(data["card"]["color"]),
                        value=Value(data["card"]["value"]),
                    )
                    chosen = Color(data["chosen_color"]) if data.get("chosen_color") else None
                    new_state = state.play_card(user_id, card, chosen)
                except (ValueError, KeyError) as e:
                    await manager.send_to_user(room_code, user_id, {
                        "type": "error", "message": str(e)
                    })
                    continue
                # 🆕 MULTI-PLACEMENT: player oxirgi kartasini tashlagan bo'lsa placement e'lon qilinadi
                if user_id in state.finish_order:
                    info = await _user_info(user_id)
                    place = state.finish_order.index(user_id) + 1
                    await manager.broadcast(room_code, {
                        "type": "player_finished", "user_id": user_id,
                        "name": info["name"], "place": place,
                        "message": f"🏆 {info['name']} — {place}-o'rin!",
                    })
                await manager.broadcast(room_code, {"type": "game_update"})
                await _send_personalized_state(room_code, state)
                if state.winner_id:
                    await _finish_game(room_code, state)
                else:
                    _blitz_arm(room_code, state)
                    import asyncio as _a
                    _a.ensure_future(_maybe_bot_turn(room_code, state))
                continue

            if action == "draw_card" and user_id is not None:
                state = games.get(room_code)
                if not state:
                    continue
                # 🆕 Stacking: pending penalti bo'lsa oddiy draw emas — TO'LIQ penalti olinadi
                # (client draw_count yubormaydi; server pending_draw ni o'zi hisoblaydi)
                if state.pending_draw > 0:
                    try:
                        state.take_penalty(user_id)
                    except ValueError as e:
                        await manager.send_to_user(room_code, user_id, {
                            "type": "error", "message": str(e)
                        })
                        continue
                    await manager.broadcast(room_code, {
                        "type": "penalty_taken", "user_id": user_id,
                        "message": "📥 " + str(user_id) + " penaltini o'tadi — navbat o'tadi",
                    })
                    await manager.broadcast(room_code, {"type": "game_update"})
                    await _send_personalized_state(room_code, state)
                    _blitz_arm(room_code, state)
                    import asyncio as _a
                    _a.ensure_future(_maybe_bot_turn(room_code, state))
                    continue
                try:
                    state.draw_cards(user_id, 1)
                except ValueError as e:
                    await manager.send_to_user(room_code, user_id, {
                        "type": "error", "message": str(e)
                    })
                    continue
                await manager.broadcast(room_code, {"type": "game_update"})
                await _send_personalized_state(room_code, state)
                _blitz_arm(room_code, state)
                import asyncio as _a
                _a.ensure_future(_maybe_bot_turn(room_code, state))
                continue

            # 🆕 Stacking rule 13: player penaltini ixtiyoriy qabul qiladi ("TAKE PENALTY" tugmasi)
            if action == "take_penalty" and user_id is not None:
                state = games.get(room_code)
                if not state:
                    continue
                try:
                    state.take_penalty(user_id)
                except ValueError as e:
                    await manager.send_to_user(room_code, user_id, {
                        "type": "error", "message": str(e)
                    })
                    continue
                await manager.broadcast(room_code, {
                    "type": "penalty_taken", "user_id": user_id,
                    "message": "📥 Penalti olindi — navbat o'tadi",
                })
                await manager.broadcast(room_code, {"type": "game_update"})
                await _send_personalized_state(room_code, state)
                _blitz_arm(room_code, state)
                import asyncio as _a
                _a.ensure_future(_maybe_bot_turn(room_code, state))
                continue

            # 🆕 SWAP card: tashlagan o'yinchi tanlagan target bilan qo'llarni almashtiradi
            if action == "swap_players" and user_id is not None:
                state = games.get(room_code)
                if not state:
                    continue
                target_id = data.get("target_id")
                try:
                    state.swap_players(user_id, int(target_id))
                except (ValueError, TypeError) as e:
                    await manager.send_to_user(room_code, user_id, {
                        "type": "error", "message": str(e)
                    })
                    continue
                await manager.broadcast(room_code, {
                    "type": "swap_done", "by": user_id, "target": int(target_id),
                    "message": "🔄 Kartalar almashtirildi!",
                })
                await manager.broadcast(room_code, {"type": "game_update"})
                await _send_personalized_state(room_code, state)
                continue

            # Drawn card is playable -> player throws it immediately (draw-then-play)
            if action == "play_drawn" and user_id is not None:
                state = games.get(room_code)
                if not state or not state.drew_playable:
                    continue
                try:
                    from .game.cards import Card, Color
                    player = state._find_player(user_id)
                    card = player.hand[-1]
                    card = Card(color=card.color, value=card.value)
                    chosen = Color(data["chosen_color"]) if data.get("chosen_color") else None
                    state.play_card(user_id, card, chosen)
                except (ValueError, KeyError) as e:
                    await manager.send_to_user(room_code, user_id, {
                        "type": "error", "message": str(e)
                    })
                    continue
                await manager.broadcast(room_code, {"type": "game_update"})
                await _send_personalized_state(room_code, state)
                if state.winner_id:
                    await _finish_game(room_code, state)
                else:
                    _blitz_arm(room_code, state)
                    import asyncio as _a
                    _a.ensure_future(_maybe_bot_turn(room_code, state))
                continue

            # Keep (drawn) card: end the drawing player's turn
            if action == "keep_card" and user_id is not None:
                state = games.get(room_code)
                if not state or not state.drew_playable:
                    continue
                state.drew_playable = False
                state._advance_turn()
                await manager.broadcast(room_code, {"type": "game_update"})
                await _send_personalized_state(room_code, state)
                _blitz_arm(room_code, state)
                import asyncio as _a
                _a.ensure_future(_maybe_bot_turn(room_code, state))
                continue

            if action == "call_uno" and user_id is not None:
                state = games.get(room_code)
                if state:
                    try:
                        state.call_uno(user_id)
                    except ValueError as e:
                        await manager.send_to_user(room_code, user_id, {
                            "type": "error", "message": str(e)
                        })
                continue

            # --- reactions (emoji burst) ---
            if action == "react" and user_id is not None:
                await manager.broadcast(room_code, {
                    "type": "reaction",
                    "user_id": user_id,
                    "name": manager.user_names.get(user_id, "O'yinchi"),
                    "emoji": str(data.get("emoji", "👍"))[:8],
                })
                continue

            # --- in-game chat (works in lobby AND during the game) ---
            if action == "chat":
                text = str(data.get("text", ""))[:300]
                if text.strip():
                    async with async_session_maker() as db:
                        meta = await _chat_meta(db, user_id)
                    if meta["blocked"]:
                        continue
                    if meta["muted"]:
                        if user_id is not None:
                            await manager.send_to_user(room_code, user_id, {
                                "type": "muted",
                                "muted_until": meta.get("muted_until"),
                            })
                        continue
                    await manager.broadcast(room_code, {
                        "type": "chat",
                        "room": room_code,
                        "msg_id": data.get("msg_id"),  # client id — sender optimistic xabari bilan dedupe
                        "user_id": data.get("user_id") or user_id,
                        "name": data.get("name") or manager.user_names.get(user_id, "O'yinchi") if user_id else (data.get("name") or "O'yinchi"),
                        "role": meta["role"],
                        "cosmetics": meta.get("cosmetics"),
                        "text": text.strip(),
                    })
                continue

            # Unknown actions are ignored (no more blind echo)
    except WebSocketDisconnect:
        manager.disconnect(room_code, websocket)
        # 🆕 RECONNECT WINDOW: WS uzildi — 5 daqiqa reconnect muddati (game active bo'lsa)
        if user_id is not None:
            _schedule_disconnect(room_code, user_id)
    except Exception:
        import logging, traceback
        logging.getLogger("uvicorn.error").error(
            "WS handler error in room %s: %s", room_code, traceback.format_exc()
        )
        manager.disconnect(room_code, websocket)
        if user_id is not None:
            _schedule_disconnect(room_code, user_id)


# In-memory UNO games: room_code -> GameState
# (single-process deployment; restart clears running games)
games: dict[str, GameState] = {}

# Rooms whose game_over has already been recorded (double-count guard)
_finished_rooms: set[str] = set()

# 🆕 RECONNECT WINDOW: (room_code, user_id) -> asyncio.TimerHandle
# Player WS dan uzilganda 5 daqiqa reconnect muddati; bu vaqt ichida game uni o'yindan chiqarmaydi.
RECONNECT_WINDOW_SECONDS = 300
_disconnect_timers: dict = {}


def _cancel_disconnect_timer(room_code: str, user_id: int) -> None:
    """Reconnect window timer'ni bekor qilish (reconnect bo'lganda)."""
    import asyncio as _asyncio
    key = (room_code, user_id)
    handle = _disconnect_timers.pop(key, None)
    if handle is not None:
        handle.cancel()


def _schedule_disconnect(room_code: str, user_id: int) -> None:
    """Player WS dan uzilganda 5 daqiqalik reconnect window o'rnatadi.
    Race-safe: eski timer bo'lsa avval bekor qilinadi (idempotent)."""
    import asyncio as _asyncio
    key = (room_code, user_id)
    old = _disconnect_timers.pop(key, None)
    if old is not None:
        old.cancel()
    state = games.get(room_code)
    if not state or state.winner_id:
        return
    if user_id not in state.active_player_ids():
        return  # allaqachon tugatgan/chiqib ketgan — window kerak emas
    loop = _asyncio.get_event_loop()
    _disconnect_timers[key] = loop.call_later(
        RECONNECT_WINDOW_SECONDS,
        lambda: _asyncio.ensure_future(_reconnect_timeout(room_code, user_id)),
    )


async def _reconnect_timeout(room_code: str, user_id: int) -> None:
    """🆕 5 daqiqa ichida qaytalmagan player o'yinni tark etadi (placement oladi).
    Idempotent: withdraw_player ikki marta chaqirilsa zarar yetkazmaydi."""
    _disconnect_timers.pop((room_code, user_id), None)
    state = games.get(room_code)
    if not state or state.winner_id:
        return
    if user_id not in state.active_player_ids():
        return
    state.withdraw_player(user_id)
    info = await _user_info(user_id)
    await manager.broadcast(room_code, {
        "type": "player_left", "user_id": user_id, "name": info["name"],
        "reason": "reconnect_timeout",
        "message": f"⏰ {info['name']} o'yinga qaytmadi",
    })
    await manager.broadcast(room_code, {"type": "game_update"})
    await _send_personalized_state(room_code, state)
    if state.winner_id:
        await _finish_game(room_code, state)


async def _finish_game(room_code: str, state: GameState):
    """Record the finished game exactly once, with placements from finish_order.
    🆕 MULTI-PLACEMENT: finish_order endi barcha o'rinlarni saqlaydi (1st, 2nd, 3rd...)
    — playerlar ketma-ket tugatadi va game oxirgi active player qolganda finalize bo'ladi.
    🆕 Game tugaganda barcha blitz/reconnect timerlari bekor qilinadi.
    """
    if room_code in _finished_rooms:
        return
    _finished_rooms.add(room_code)
    games.pop(room_code, None)
    # 🆕 barcha reconnect window timerlarini bekor qilish (game tugadi)
    for key in [k for k in _disconnect_timers if k[0] == room_code]:
        handle = _disconnect_timers.pop(key, None)
        if handle is not None:
            handle.cancel()

    # 🆕 Placements: finish_order to'liq tartibni beradi (withdrawn oxirida bo'lishi mumkin).
    # Xavfsizlik uchun: finish_order'da yo'q playerlar qo'shiladi (idempotent).
    placement_ids = list(state.finish_order)
    for p in state.players:
        if p.user_id not in placement_ids:
            placement_ids.append(p.user_id)
    standings = []
    for i, uid in enumerate(placement_ids):
        info = await _user_info(uid)
        standings.append({
            "place": i + 1,
            "user_id": uid,
            "name": info["name"],
            "cards_left": next((len(p.hand) for p in state.players if p.user_id == uid), 0),
        })
    winner = standings[0] if standings else None
    # Rejimli xona prize: g'olib hammasini oladi
    prize = 0
    try:
        async with async_session_maker() as db:
            from .models.room import Room
            from .models.social import OnlineUser
            room = await db.get(Room, room_code)
            if room and getattr(room, 'prize', 0):
                prize = getattr(room, 'prize', 0) or 0
                if prize > 0 and winner:
                    w = await db.get(OnlineUser, winner["user_id"])
                    if w:
                        w.coins = (w.coins or 0) + prize
                        from .api.social import _broadcast_stats_update as _bsu_prize
                        try:
                            await _bsu_prize(db, winner["user_id"])
                        except Exception:
                            pass
                # prize berilgandan keyin xona prize'ni nollaymiz (takror berilmasligi uchun)
                if room:
                    room.prize = 0
                    await db.commit()
    except Exception:
        import logging, traceback
        logging.getLogger("uvicorn.error").error("prize distribute failed: %s", traceback.format_exc())
    await manager.broadcast(room_code, {
        "type": "game_over",
        "winner": winner,
        "standings": standings,
        "prize": prize,
    })
    # DB: one GameRecord per game, stats updated server-side once
    try:
        from .api.social import record_result_server
        duration = None
        if state.started_at:
            duration = int((now_local() - state.started_at).total_seconds())
        await record_result_server(
            room_code=room_code,
            winner_id=winner["user_id"] if winner else None,
            winner_name=winner["name"] if winner else None,
            players=[{"id": s["user_id"], "name": s["name"], "place": s["place"], "cards_left": s["cards_left"]} for s in standings],
            started_at=state.started_at,
            duration_sec=duration,
            draw_count=getattr(state, "draw_count", 0),
            turn_count=getattr(state, "turn_count", 0),
        )
    except Exception:
        import logging, traceback
        logging.getLogger("uvicorn.error").error("record_result_server failed: %s", traceback.format_exc())


async def _user_info(user_id: int) -> dict:
    name = manager.user_names.get(user_id)
    cosmetics = None
    if not name:
        async with async_session_maker() as db:
            from .models.social import OnlineUser
            u = await db.get(OnlineUser, user_id)
            name = (u.first_name or u.username) if u else None
            if u:
                try:
                    from .api.social import _cosmetics_of as _c_of
                    cosmetics = _c_of(u)
                except Exception:
                    cosmetics = None
    else:
        # also fetch cosmetics even if name cached
        async with async_session_maker() as db:
            from .models.social import OnlineUser
            u = await db.get(OnlineUser, user_id)
            if u:
                try:
                    from .api.social import _cosmetics_of as _c_of
                    cosmetics = _c_of(u)
                except Exception:
                    cosmetics = None
    return {"id": user_id, "name": name or "O'yinchi", "cosmetics": cosmetics}


async def _send_personalized_state(room_code: str, state: GameState):
    """Send each player their own hand; others see only card counts.
    Bitta user'dagi xato (masalan kosmetika) butun broadcast'ni buzmasligi uchun
    har bir qadam try/except bilan himoyalangan — aks holda kartalar hech kimga ko'rinmay qoladi."""
    try:
        top = state.discard_pile[-1].serialize() if state.discard_pile else None
    except Exception:
        top = None
    full_players = []
    for p in state.players:
        try:
            info = await _user_info(p.user_id)
        except Exception:
            info = {"name": "O'yinchi", "cosmetics": None}
        try:
            hand_count = len(p.hand)
        except Exception:
            hand_count = 0
        full_players.append({
            "user_id": p.user_id,
            "username": (info.get("name") if isinstance(info, dict) else "O'yinchi"),
            "hand_count": hand_count,
            "called_uno": bool(getattr(p, "called_uno", False)),
            "cosmetics": (info.get("cosmetics") if isinstance(info, dict) else None),
        })
    try:
        current_pid = state.current_player().user_id
    except Exception:
        current_pid = None
    try:
        deck_remaining = state.deck.remaining()
    except Exception:
        deck_remaining = 0
    for p in state.players:
        try:
            hand = [c.serialize() for c in p.hand]
        except Exception:
            hand = []
        payload = {
            "type": "game_state",
            "state": {
                "players": full_players,
                "hand": hand,
                "current_player_id": current_pid,
                "direction": getattr(state, "direction", "clockwise"),
                "top_card": top,
                "deck_remaining": deck_remaining,
                "winner_id": getattr(state, "winner_id", None),
                "pending_draw": getattr(state, "pending_draw", 0),
                "drew_playable": bool(getattr(state, "drew_playable", False)),
                "pending_swap_by": getattr(state, "pending_swap_by", None),
                "finish_order": list(getattr(state, "finish_order", []) or []),
                "withdrawn": list(getattr(state, "withdrawn", []) or []),
                "active_players": list(state.active_player_ids()) if hasattr(state, "active_player_ids") else [],
            },
        }
        try:
            await manager.send_to_user(room_code, p.user_id, payload)
        except Exception:
            pass
