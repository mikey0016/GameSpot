# test_api_mafia.py — REST API + Mafia WS oqimi integratsiya testi
# REST: httpx ASGITransport; WS: websocket_endpoint to'g'ridan-to'g'ri FakeWebSocket bilan.
# (starlette TestClient bu muhitda osiladi — anyio/Windows muvofiqlik muammosi)
"""Ishga tushirish: py -3.12 test_api_mafia.py"""
import os
os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ.setdefault("BOT_USERNAME", "test_bot")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("WEBAPP_URL", "http://localhost:3000")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_api_check.db")

for _f in ("test_api_check.db",):
    try:
        os.remove(_f)
    except OSError:
        pass

import asyncio
import sys

import httpx

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


_CLOSE = object()


class FakeWebSocket:
    """websocket_endpoint'ni to'g'ridan-to'g'ri sinov uchun stub."""

    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: asyncio.Queue = asyncio.Queue()
        self.history: list = []  # barcha kelgan xabarlar (tekshiruv uchun)
        self.accepted = False
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, data):
        if not self.closed:
            self.history.append(data)
            await self.sent.put(data)

    async def receive_json(self):
        item = await self.incoming.get()
        if item is _CLOSE:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect(code=1000)
        return item

    async def close(self, code=1000):
        self.closed = True
        await self.incoming.put(_CLOSE)

    # --- test yordamchilari ---
    def send(self, data):
        self.incoming.put_nowait(data)

    async def recv(self, timeout=6):
        try:
            return await asyncio.wait_for(self.sent.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def wait_for_types(self, types, max_msgs=50, timeout=12):
        """types turlaridan biri (yoki hammasi) kelguncha o'qiydi."""
        got = {t: False for t in types}
        for _ in range(max_msgs):
            msg = await self.recv(timeout)
            if msg is None:
                break
            t = msg.get("type")
            if t in got:
                got[t] = True
            if all(got.values()):
                break
        return got


async def run_tests():
    from backend.main import app, websocket_endpoint
    from backend.database import init_db

    await init_db()  # test bazasi jadvallari

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ---------- REST: xona yaratish (mafia) ----------
        print("== REST: MAFIA XONA ==")
        r = await client.post("/rooms/create", json={
            "host_id": 101, "game": "mafia", "max_players": 6,
        })
        check("create mafia room 200", r.status_code == 200)
        room = r.json()
        check("game=mafia saqlandi", room.get("game") == "mafia")
        code = room["code"]
        check("kod 6 belgi", len(code) == 6)

        r = await client.post("/rooms/create", json={"host_id": 102, "game": "mafia", "max_players": 50})
        check("max_players 50 -> 12 clamp", r.json().get("max_players") == 12)

        r = await client.post("/rooms/create", json={"host_id": 103, "game": "uno", "max_players": 12})
        check("uno max_players 12 -> 7 clamp", r.json().get("max_players") == 7)

        r = await client.post("/rooms/create", json={"host_id": 104, "game": "fortnite"})
        check("noto'g'ri game -> uno", r.json().get("game") == "uno")

        r = await client.post("/rooms/create", json={"host_id": 105})
        check("game yo'q -> uno default", r.json().get("game") == "uno")

        # ---------- REST: qo'shilish ----------
        print("== REST: QO'SHILISH ==")
        r = await client.post(f"/rooms/join/{code}", json={"user_id": 201})
        check("join 200", r.status_code == 200)
        check("game badyda bor", r.json().get("game") == "mafia")
        check("2 o'yinchi", len(r.json()["players"]) == 2)

        await client.post(f"/rooms/join/{code}", json={"user_id": 202})
        r = await client.post(f"/rooms/join/{code}", json={"user_id": 203})
        check("4 o'yinchi", len(r.json()["players"]) == 4)

        # ---------- REST: /rooms/open filtri ----------
        print("== REST: OPEN FILTER ==")
        r = await client.get("/rooms/open?game=mafia")
        rooms_m = r.json()
        check("open?game=mafia faqat mafia", all(x["game"] == "mafia" for x in rooms_m))
        check("mafia xona ro'yxatda", any(x["code"] == code for x in rooms_m))

        r = await client.get("/rooms/open?game=uno")
        check("open?game=uno faqat uno", all(x["game"] == "uno" for x in r.json()))

        r = await client.get("/rooms/open")
        check("opensiz: hammasi", len(r.json()) >= len(rooms_m))

        r = await client.get(f"/rooms/{code}")
        check("room status game bor", r.json().get("game") == "mafia")

        # ---------- WS: mafia o'yin oqimi ----------
        print("== WS: MAFIA OQIMI ==")
        users = [101, 201, 202, 203]
        sockets = {}

        # websocket_endpoint'larni background task sifatida ishga tushirish
        tasks = []
        for uid in users:
            ws = FakeWebSocket()
            sockets[uid] = ws
            tasks.append(asyncio.create_task(websocket_endpoint(code, ws)))

        # hello
        for uid in users:
            sockets[uid].send({"action": "hello", "user_id": uid, "name": f"U{uid}"})
        for uid in users:
            msg = await sockets[uid].recv(5)
            check(f"hello_ok (U{uid})", bool(msg and msg.get("type") == "hello_ok"))

        # host start_game (mafia)
        sockets[101].send({"action": "start_game"})

        ok_start, ok_personal = True, True
        roles = {}
        for uid in users:
            got = await sockets[uid].wait_for_types(["mafia_start", "mafia_personal"])
            ok_start &= got["mafia_start"]
            ok_personal &= got["mafia_personal"]
        check("hamma mafia_start oldi", ok_start)
        check("hamma personal state oldi", ok_personal)

        # rollarni tekshirish (history'dan personal state)
        for uid in users:
            found = next((m for m in reversed(sockets[uid].history)
                          if m.get("type") == "mafia_personal" and m.get("state", {}).get("me")), None)
            if found:
                roles[uid] = found["state"]["me"]["role"]
        check("4 ta rol taqsimlandi", len(roles) == 4)
        check("rollar to'g'ri", all(v in (
            "don", "mafia", "doctor", "detective", "witness", "vigilante",
            "judge", "veteran", "civilian") for v in roles.values()))
        check("kamida 1 mafia bor", any(v in ("don", "mafia") for v in roles.values()))

        # tungi harakatlar: hamma None (skip)
        for uid in users:
            sockets[uid].send({"action": "mafia_night_target", "target_id": None})

        # mafia_day (advance: night -> day; 2s sleep bor)
        ok_day = True
        for uid in users:
            got = await sockets[uid].wait_for_types(["mafia_day"], timeout=15)
            ok_day &= got["mafia_day"]
        check("hamma kun'ga o'tdi (mafia_day)", ok_day)

        # kun ovozlari: 101->201, 201->101 (teng ovoz), qolganlar betaraf
        for uid in users:
            target = {101: 201, 201: 101}.get(uid)
            sockets[uid].send({"action": "mafia_vote", "target_id": target})

        ok_result = True
        for uid in users:
            got = await sockets[uid].wait_for_types(["mafia_night", "mafia_vote_result"], timeout=15)
            ok_result &= (got["mafia_night"] or got["mafia_vote_result"])
        check("ovoz natijasi yetdi (tie->night)", ok_result)

        # xato holati: night'da trial vote -> error qaytishi kerak (crash yo'q)
        sockets[101].send({"action": "mafia_trial_vote", "verdict": "guilty"})
        err_msg = None
        for _ in range(30):
            m = await sockets[101].recv(4)
            if m is None:
                break
            if m.get("type") == "error":
                err_msg = m
                break
        check("night'da trial vote -> error (crash yo'q)",
              err_msg is not None and "sud" in str(err_msg.get("message", "")).lower())

        # WS yopish
        for uid in users:
            await sockets[uid].close()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.TimeoutError, Exception):
                t.cancel()

        # ---------- REST: leave ----------
        print("== REST: LEAVE ==")
        r = await client.post(f"/rooms/leave/{code}", json={"user_id": 203})
        check("leave 200", r.status_code == 200)
        check("3 o'yinchi qoldi", len(r.json()["players"]) == 3)

        r = await client.post(f"/rooms/leave/{code}", json={"user_id": 101})
        check("host o'tdi 201", r.json().get("host_id") == 201)

        await client.post(f"/rooms/leave/{code}", json={"user_id": 201})
        await client.post(f"/rooms/leave/{code}", json={"user_id": 202})

        # ---------- REST: kick + parolli xona (regression) ----------
        print("== REST: REGRESSION ==")
        r = await client.post("/rooms/create", json={
            "host_id": 301, "game": "uno", "password": "abc", "max_players": 4,
        })
        code2 = r.json()["code"]
        check("uno parolli yaratildi", r.json().get("has_password") is True)

        r = await client.post(f"/rooms/join/{code2}", json={"user_id": 302, "password": "wrong"})
        check("noto'g'ri parol -> 403", r.status_code == 403)

        r = await client.post(f"/rooms/join/{code2}", json={"user_id": 302, "password": "abc"})
        check("to'g'ri parol -> 200", r.status_code == 200)

        r = await client.post(f"/rooms/kick/{code2}", json={"host_id": 301, "target_id": 302})
        check("kick 200", r.status_code == 200)
        check("kick: 1 o'yinchi qoldi", len(r.json()["players"]) == 1)

        r = await client.post("/rooms/create", json={
            "host_id": 401, "game": "mafia", "entry_fee": 50, "max_players": 4,
        })
        check("entry_fee=50 saqlandi", r.json().get("entry_fee") == 50)
        check("prize=50", r.json().get("prize") == 50)


async def main():
    await run_tests()
    print(f"\nPASS: {passed}, FAIL: {failed}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
