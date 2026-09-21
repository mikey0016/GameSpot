# Test 2-player real UNO game over WebSocket
import asyncio, json, urllib.request
import websockets

API = "http://localhost:8000"
WS = "ws://localhost:8000"


def api_post(path, payload):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


async def player(name, uid, code, actions):
    ws = await websockets.connect(f"{WS}/ws/{code}")
    await ws.send(json.dumps({"action": "hello", "user_id": uid, "name": name}))
    state = None
    # collect messages for a moment
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if msg.get("type") == "game_state":
                state = msg["state"]
                if actions:
                    act = actions.pop(0)
                    await asyncio.sleep(0.3)
                    await ws.send(json.dumps(act))
                else:
                    break
    except asyncio.TimeoutError:
        pass
    await ws.close()
    return state


async def main():
    room = api_post("/rooms/create", {"host_id": 900, "max_players": 2})
    code = room["code"]
    print("room:", code)
    api_post(f"/rooms/join/{code}", {"user_id": 901})

    # Both connect; host starts
    w1 = await websockets.connect(f"{WS}/ws/{code}")
    w2 = await websockets.connect(f"{WS}/ws/{code}")
    await w1.send(json.dumps({"action": "hello", "user_id": 900, "name": "Alice"}))
    await w2.send(json.dumps({"action": "hello", "user_id": 901, "name": "Bob"}))
    await asyncio.sleep(0.3)

    await w1.send(json.dumps({"action": "start_game"}))

    # Read states
    states = {}
    for w, uid in [(w1, 900), (w2, 901)]:
        for _ in range(3):
            try:
                msg = json.loads(await asyncio.wait_for(w.recv(), timeout=3))
                if msg.get("type") == "game_state":
                    states[uid] = msg["state"]
                    break
            except asyncio.TimeoutError:
                break

    s1 = states.get(900)
    s2 = states.get(901)
    assert s1, "player 900 got no state"
    assert s2, "player 901 got no state"
    print("p900 hand:", len(s1["hand"]), "p901 hand:", len(s2["hand"]))
    assert len(s1["hand"]) == 7 and len(s2["hand"]) == 7, "hands must be 7 cards"
    assert s1["hand"] != s2["hand"], "hands must differ (personalized)"
    print("top card:", s1["top_card"])
    print("current:", s1["current_player_id"])
    print("p901 sees p900 count:", [p for p in s2["players"] if p["user_id"] == 900])

    # The current player plays a MATCHING card (or draws if none match)
    cur = s1["current_player_id"]
    w_cur = w1 if cur == 900 else w2
    s_cur = states[cur]
    top = s_cur["top_card"]
    eff_top_color = (top.get("chosen_color") or top["color"]) if top else None

    def matches(c):
        if c["color"] == "wild":
            return True
        return c["color"] == eff_top_color or c["value"] == top["value"]

    playable = [c for c in s_cur["hand"] if matches(c)]
    if playable:
        card = playable[0]
        print(f"player {cur} plays {card}")
        await w_cur.send(json.dumps({
            "action": "play_card",
            "card": {"color": card["color"], "value": card["value"]},
            "chosen_color": "red" if card["color"] == "wild" else None,
        }))
    else:
        print(f"player {cur} has no matching card, draws")
        await w_cur.send(json.dumps({"action": "draw_card"}))
    await w_cur.send(json.dumps({
        "action": "play_card",
        "card": {"color": card["color"], "value": card["value"]},
        "chosen_color": "red" if card["color"] == "wild" else None,
    }))

    # Both should receive game_update + game_state
    got_update = got_state = False
    new_state = None
    for w in [w1, w2]:
        for _ in range(6):
            try:
                msg = json.loads(await asyncio.wait_for(w.recv(), timeout=3))
                t = msg.get("type")
                if t == "game_update":
                    got_update = True
                if t == "game_state":
                    got_state = True
                    if msg["state"]["hand"]:
                        new_state = msg["state"]
                if t == "error":
                    print("ERROR:", msg["message"])
            except asyncio.TimeoutError:
                break

    print("broadcast ok:", got_update, "personal state ok:", got_state)
    if new_state:
        print("new top:", new_state["top_card"], "next turn:", new_state["current_player_id"])

    # Reaction relay test
    await w1.send(json.dumps({"action": "react", "emoji": "🔥"}))
    rx = None
    try:
        msg = json.loads(await asyncio.wait_for(w2.recv(), timeout=3))
        if msg.get("type") == "reaction":
            rx = msg
    except asyncio.TimeoutError:
        pass
    print("reaction relayed:", rx)

    # Lobby chat test
    await w1.send(json.dumps({"action": "chat", "user_id": 900, "name": "Alice", "text": "salom!"}))
    chat = None
    try:
        msg = json.loads(await asyncio.wait_for(w2.recv(), timeout=3))
        if msg.get("type") == "chat":
            chat = msg
    except asyncio.TimeoutError:
        pass
    print("chat relayed:", chat)

    await w1.close()
    await w2.close()

    if got_update and got_state and rx and chat:
        print("ALL TESTS PASSED ✅")
    else:
        print("SOME TESTS FAILED ❌")


asyncio.run(main())
