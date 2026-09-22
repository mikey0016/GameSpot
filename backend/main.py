# backend/main.py

"""FastAPI entry point for Game Spot.
It registers REST API routers, WebSocket endpoint and includes CORS configuration.
"""

from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .config import settings
from .database import async_session_maker, init_db

from .api import rooms_router, social_router  # import routers
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
            u = OnlineUser(id=admin_id, role="owner")
            db.add(u)
        u.role = "owner"
        await db.commit()


async def cleanup_stale_rooms():
    """Delete rooms abandoned before a restart/crash so no ghost rooms linger.
    A room that is still WAITING and has not been touched for a while is stale.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import delete
    from .models.room import Room, RoomStatus

    cutoff = datetime.utcnow() - timedelta(hours=2)
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
                    remaining = [str(pid) for pid in (room.player_ids or []) if str(pid) != str(user_id)]
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
    from datetime import datetime as _dt
    u = await db.get(OnlineUser, user_id)
    blocked = bool(u and u.blocked)
    # Muddati chiqqan blokni avtomatik o'chirish
    if blocked and u and u.blocked_until and u.blocked_until <= _dt.utcnow():
        u.blocked = 0
        u.blocked_until = None
        u.block_reason = None
        await db.commit()
        blocked = False
    muted = bool(u and u.muted)
    # Muddati chiqqan mute'ni avtomatik o'chirish
    if muted and u and u.muted_until and u.muted_until <= _dt.utcnow():
        u.muted = 0
        u.muted_until = None
        await db.commit()
        muted = False
    return {
        "role": (u.role if u else None),
        "blocked": blocked,
        "blocked_until": (u.blocked_until.isoformat() if u and u.blocked_until else None),
        "block_reason": (u.block_reason if u else None),
        "muted": muted,
        "muted_until": (u.muted_until.isoformat() if u and u.muted_until else None),
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
            if data.get("action") == "chat":
                text = str(data.get("text", ""))[:300]
                if text.strip():
                    uid = data.get("user_id")
                    async with async_session_maker() as db:
                        meta = await _chat_meta(db, uid)
                        if not meta["blocked"]:
                            # Persist so history survives reconnects/reloads
                            from .models.social import ChatMessage
                            db.add(ChatMessage(
                                user_id=uid if isinstance(uid, int) else 0,
                                name=data.get("name") or "O'yinchi",
                                text=text.strip(),
                            ))
                            await db.commit()
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
                        "user_id": uid,
                        "name": data.get("name") or "O'yinchi",
                        "role": meta["role"],
                        "text": text.strip(),
                        "ts": datetime.utcnow().isoformat(),
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
                    state = GameState(player_ids=list(room.player_ids))
                except ValueError:
                    continue
                games[room_code] = state
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
                await manager.broadcast(room_code, {"type": "game_update"})
                await _send_personalized_state(room_code, state)
                if state.winner_id:
                    await _finish_game(room_code, state)
                continue

            if action == "draw_card" and user_id is not None:
                state = games.get(room_code)
                if not state:
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
                        "user_id": data.get("user_id") or user_id,
                        "name": data.get("name") or manager.user_names.get(user_id, "O'yinchi") if user_id else (data.get("name") or "O'yinchi"),
                        "role": meta["role"],
                        "text": text.strip(),
                    })
                continue

            # Unknown actions are ignored (no more blind echo)
    except WebSocketDisconnect:
        manager.disconnect(room_code, websocket)
    except Exception:
        import logging, traceback
        logging.getLogger("uvicorn.error").error(
            "WS handler error in room %s: %s", room_code, traceback.format_exc()
        )
        manager.disconnect(room_code, websocket)


# In-memory UNO games: room_code -> GameState
# (single-process deployment; restart clears running games)
games: dict[str, GameState] = {}

# Rooms whose game_over has already been recorded (double-count guard)
_finished_rooms: set[str] = set()


async def _finish_game(room_code: str, state: GameState):
    """Record the finished game exactly once, with placements from finish_order."""
    if room_code in _finished_rooms:
        return
    _finished_rooms.add(room_code)
    games.pop(room_code, None)

    # Placements: 1st = winner, then finish_order (UNO players), others by hand size
    remaining = [p for p in state.players if p.user_id not in state.finish_order]
    remaining.sort(key=lambda p: len(p.hand))
    placement_ids = state.finish_order + [p.user_id for p in remaining]
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
    await manager.broadcast(room_code, {
        "type": "game_over",
        "winner": winner,
        "standings": standings,
    })
    # DB: one GameRecord per game, stats updated server-side once
    try:
        from .api.social import record_result_server
        await record_result_server(
            room_code=room_code,
            winner_id=winner["user_id"] if winner else None,
            winner_name=winner["name"] if winner else None,
            players=[{"id": s["user_id"], "name": s["name"], "place": s["place"]} for s in standings],
        )
    except Exception:
        import logging, traceback
        logging.getLogger("uvicorn.error").error("record_result_server failed: %s", traceback.format_exc())


async def _user_info(user_id: int) -> dict:
    name = manager.user_names.get(user_id)
    if not name:
        async with async_session_maker() as db:
            from .models.social import OnlineUser
            u = await db.get(OnlineUser, user_id)
            name = (u.first_name or u.username) if u else None
    return {"id": user_id, "name": name or "O'yinchi"}


async def _send_personalized_state(room_code: str, state: GameState):
    """Send each player their own hand; others see only card counts."""
    top = state.discard_pile[-1].serialize() if state.discard_pile else None
    full_players = []
    for p in state.players:
        info = await _user_info(p.user_id)
        full_players.append({
            "user_id": p.user_id,
            "username": info["name"],
            "hand_count": len(p.hand),
            "called_uno": p.called_uno,
        })
    for p in state.players:
        payload = {
            "type": "game_state",
            "state": {
                "players": full_players,
                "hand": [c.serialize() for c in p.hand],
                "current_player_id": state.current_player().user_id,
                "direction": state.direction,
                "top_card": top,
                "deck_remaining": state.deck.remaining(),
                "winner_id": state.winner_id,
                "pending_draw": state.pending_draw,
            },
        }
        await manager.send_to_user(room_code, p.user_id, payload)
