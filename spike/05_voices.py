"""Phase 0 / item 5: which Realtime voices exist on gpt-realtime-2.1, and what do they sound like?

1. Probe: send an invalid voice and read the server's error, which enumerates the valid voices.
2. For each valid voice, open a session, have it read the SAME §6 disclosure line, save out/voices/<voice>.wav and
   time-to-first-audio. (A Realtime voice cannot change once the model has produced audio, hence one session each.)
David listens to the WAVs and picks; nothing here is a judgement of taste.
"""

import asyncio
import base64
import json
import re
import sys
import time

import websockets

import harness as h

MODEL = "gpt-realtime-2.1"
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
VOICE_DIR = h.OUT / "voices"
VOICE_DIR.mkdir(exist_ok=True)
LINE = ("Hi Dana, I'm Avery, IN DEMAND's AI screening assistant. This is a short first conversation about the "
        "Electrical Engineering Supervisor role. It's recorded so Stefanie's team can review it personally, "
        "and a human reviews every interview. Shall we start?")


async def connect():
    return await websockets.connect(URL, additional_headers={"Authorization": f"Bearer {h.api_key()}"}, max_size=None)


async def recv_until(ws, types, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time()))
        if evt["type"] in types:
            return evt


async def probe_valid_voices() -> list[str]:
    async with await connect() as ws:
        await recv_until(ws, {"session.created"})
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "model": MODEL, "audio": {"output": {"voice": "definitely_not_a_voice"}}}}))
        evt = await recv_until(ws, {"error", "session.updated"})
        print("probe response type:", evt["type"])
        msg = evt.get("error", {}).get("message", "")
        print("server message:", msg)
        return re.findall(r"'([a-z]+)'", msg.split("Supported values are:")[-1]) if "Supported values" in msg else []


async def record_voice(voice: str) -> dict:
    async with await connect() as ws:
        await recv_until(ws, {"session.created"})
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "model": MODEL, "output_modalities": ["audio"],
            "instructions": "Read the user's text aloud EXACTLY as written, in a warm, natural, conversational "
                            "Canadian-professional tone. Say nothing else.",
            "audio": {"output": {"voice": voice}}}}))
        evt = await recv_until(ws, {"error", "session.updated"})
        if evt["type"] == "error":
            return {"voice": voice, "ok": False, "error": evt["error"].get("message", "")[:200]}
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": LINE}]}}))
        t0 = time.time()
        await ws.send(json.dumps({"type": "response.create"}))
        pcm = bytearray()
        first = None
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if evt["type"] == "response.output_audio.delta":
                first = first or (time.time() - t0)
                pcm.extend(base64.b64decode(evt["delta"]))
            elif evt["type"] == "response.done":
                break
            elif evt["type"] == "error":
                return {"voice": voice, "ok": False, "error": evt["error"].get("message", "")[:200]}
        h.write_wav(VOICE_DIR / f"{voice}.wav", bytes(pcm), 24000)
        return {"voice": voice, "ok": True, "seconds": round(len(pcm) / 48000, 1), "ttfa_s": round(first or -1, 2),
                "file": f"out/voices/{voice}.wav"}


async def main() -> None:
    listed = await probe_valid_voices()
    print("voices enumerated by server:", listed)
    voices = listed or ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"]
    rows = []
    for v in voices:
        row = await record_voice(v)
        rows.append(row)
        print(json.dumps(row))
    (h.OUT / "05_voices.json").write_text(json.dumps({"enumerated": listed, "rows": rows}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
