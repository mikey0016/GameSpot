"""WS global chat saqlanishini tekshirish: ulanib xabar yuboradi, keyin REST tarixdan qidiradi."""
import asyncio
import json

import httpx
import websockets

BASE = "http://localhost:8000"
WS = "ws://localhost:8000/ws/global"
MARK = "salom-test-12345"


async def main() -> None:
    async with websockets.connect(WS) as ws:
        await ws.send(json.dumps({
            "action": "chat",
            "user_id": 999002,
            "name": "TestUser",
            "text": MARK,
        }))
        # javob (broadcast) kelishini kutamiz
        for _ in range(5):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(msg)
                if data.get("type") == "chat" and data.get("text") == MARK:
                    print("WS broadcast OK:", data["name"], "-", data["text"])
                    break
            except asyncio.TimeoutError:
                print("broadcast kutilmadi (davom etamiz)")
                break
    await asyncio.sleep(1)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{BASE}/chat/global")
        messages = r.json().get("messages", [])
        found = any(m.get("text") == MARK for m in messages)
        print("DB ga saqlandi:", "HA" if found else "YO'Q")
        if not found:
            raise SystemExit(1)


asyncio.run(main())
