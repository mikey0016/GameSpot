# backend/main.py

"""FastAPI entry point for Game Spot.
It registers REST API routers, WebSocket endpoint and includes CORS configuration.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .config import settings
from .database import async_session_maker, init_db

from .api import rooms_router  # import routers
# Future routers can be added here, e.g., from .api import stats_router, admin_router
# from .api import rooms_router, stats_router, admin_router

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

# Register API routers
app.include_router(rooms_router)

# ------------------- WebSocket manager placeholder -------------------

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, set[WebSocket]] = {}

    async def connect(self, room_code: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(room_code, set()).add(websocket)

    def disconnect(self, room_code: str, websocket: WebSocket):
        self.active_connections.get(room_code, set()).discard(websocket)
        if not self.active_connections.get(room_code):
            self.active_connections.pop(room_code, None)

    async def broadcast(self, room_code: str, message: dict):
        for connection in self.active_connections.get(room_code, []):
            await connection.send_json(message)

manager = ConnectionManager()

@app.websocket("/ws/{room_code}")
async def websocket_endpoint(room_code: str, websocket: WebSocket, token: str = ""):
    # TODO: validate Telegram initData token here
    await manager.connect(room_code, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # For now just echo back to room
            await manager.broadcast(room_code, data)
    except WebSocketDisconnect:
        manager.disconnect(room_code, websocket)

# -------------------------------------------------------------------

# Note: Actual game logic, routers and services will be added later.
