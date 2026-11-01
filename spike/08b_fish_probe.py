import os, json, asyncio, httpx, websockets, ormsgpack
from dotenv import load_dotenv
load_dotenv(override=True)
K = os.environ["FISH_API_KEY"]
H = {"Authorization": f"Bearer {K}"}
for path in ["/wallet/self/api-credit", "/wallet/self/package"]:
    r = httpx.get("https://api.fish.audio" + path, headers=H, timeout=20)
    body = r.text[:300]
    print(path, r.status_code, body)
async def ws_try(model):
    try:
        async with websockets.connect("wss://api.fish.audio/v1/tts/live", additional_headers={**H, "model": model}) as ws:
            await ws.send(ormsgpack.packb({"event": "start", "request": {"text": "", "format": "pcm", "sample_rate": 24000, "reference_id": "e3cd384158934cc9a01029cd7d278634"}}))
            await ws.send(ormsgpack.packb({"event": "text", "text": "Hello there. "}))
            await ws.send(ormsgpack.packb({"event": "flush"}))
            n = 0
            for _ in range(20):
                m = ormsgpack.unpackb(await asyncio.wait_for(ws.recv(), 10))
                if m.get("event") == "audio": n += len(m["audio"])
                if m.get("event") in ("finish", "error"): print(model, "ws event", m.get("event"), {k: v for k, v in m.items() if k != "audio"}); break
            print(model, "ws audio bytes", n)
    except Exception as e:
        print(model, "ws EXC", type(e).__name__, str(e)[:200])
for m in ["s2.1-pro", "s2.1-pro-free"]:
    asyncio.run(ws_try(m))
for m in ["s2.1-pro", "s2.1-pro-free"]:
    r = httpx.post("https://api.fish.audio/v1/tts", headers={**H, "model": m, "content-type": "application/json"},
                   json={"text": "Hello there.", "format": "wav", "reference_id": "e3cd384158934cc9a01029cd7d278634"}, timeout=40)
    print(m, "REST", r.status_code, r.headers.get("content-type"), len(r.content), (r.text[:300] if r.status_code != 200 else ""))
