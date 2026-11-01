"""VAD A/B on REAL recorded mic audio: replay a session wav in real time through the raw OpenAI realtime websocket under
several turn-detection configs (create_response=false, no model output) and compare speech_started/stopped to Silero.
usage: 25_vad_ab.py <session-stem>"""
import asyncio, base64, json, sys, time, wave
import numpy as np, soxr, websockets
from dotenv import load_dotenv
from loguru import logger
load_dotenv("../.env", override=True)
logger.remove()
import os
from pipecat.audio.vad.silero import SileroVADAnalyzer
import bakeoff_common as b

stem = sys.argv[1]
w = wave.open(f"out/{stem}_mic.wav"); pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16); assert w.getframerate() == 24000
CONFIGS = {  # name: (threshold, near_field NR, silence_ms)
    "T0.65+NR": (0.65, True, 600), "T0.50+NR": (0.5, True, 600), "T0.50 noNR": (0.5, False, 600), "T0.65 noNR": (0.65, False, 600),
}

async def run(name, thr, nr, sil):
    ev = []
    async with websockets.connect("wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
                                  additional_headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, max_size=None) as ws:
        while json.loads(await ws.recv())["type"] != "session.created":
            pass
        inp = {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": {"type": "server_vad", "threshold": thr, "silence_duration_ms": sil, "create_response": False}}
        if nr:
            inp["noise_reduction"] = {"type": "near_field"}
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime", "model": "gpt-realtime-2.1", "output_modalities": ["audio"], "audio": {"input": inp}}}))
        t0 = None
        async def reader():
            async for raw in ws:
                e = json.loads(raw)
                if e["type"] in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped") and t0:
                    ev.append((e["type"].split(".")[-1], time.perf_counter() - t0))
                elif e["type"] == "error":
                    print(name, "ERROR", e["error"].get("message", "")[:150])
        rt = asyncio.create_task(reader()); await asyncio.sleep(1.0)
        t0 = time.perf_counter(); t = t0
        for i in range(0, len(pcm) + 24000 * 3, 480):  # +3 s tail silence
            chunk = pcm[i:i + 480]
            if len(chunk) < 480: chunk = np.pad(chunk, (0, 480 - len(chunk)))
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk.astype(np.int16).tobytes()).decode()}))
            t += 0.02; await asyncio.sleep(max(0, t - time.perf_counter()))
        await asyncio.sleep(1.5); rt.cancel()
    return ev

async def main():
    res = await asyncio.gather(*(run(n, *c) for n, c in CONFIGS.items()))
    # Silero utterances: speech chunks merged across gaps < 1.0 s (the end of an utterance = a >= 1.0 s gap)
    x = soxr.resample(pcm, 24000, 16000).astype(np.int16); v = SileroVADAnalyzer(); v.set_sample_rate(16000); st = 512 / 16000
    pr = [float(np.squeeze(v.voice_confidence(x[i:i + 512].tobytes()))) for i in range(0, len(x) - 512, 512)]
    segs = []
    for i, q in enumerate(pr):
        if q > 0.5:
            t = i * st
            if segs and t - segs[-1][1] < 1.0: segs[-1][1] = t + st
            else: segs.append([t, t + st])
    segs = [s for s in segs if s[1] - s[0] >= 0.5]
    print("Silero utterances (>=0.5 s):", [(round(a, 1), round(b, 1)) for a, b in segs])
    out = {}
    for (name, _), ev in zip(CONFIGS.items(), res):
        stops = [t for k, t in ev if k == "speech_stopped"]; starts = [t for k, t in ev if k == "speech_started"]
        lags, missed = [], 0
        for a, bnd in segs:
            nxt = [s for s in stops if s >= bnd]
            lags.append(round(nxt[0] - bnd, 2) if nxt else None)
            if not any(a - 0.5 <= s <= bnd for s in starts): missed += 1
        false_open = sum(1 for s in starts if not any(a - 1 <= s <= bnd + 0.5 for a, bnd in segs))
        vals = [l for l in lags if l is not None]
        out[name] = {"turns_opened": len(starts), "stop_lag_s": lags, "p50": b.pct(vals, 50), "max": max(vals, default=None), "utterances_missed": missed, "false_opens": false_open}
        print(name, json.dumps(out[name]))
    json.dump({"segs": segs, "configs": out, "events": {n: e for (n, _), e in zip(CONFIGS.items(), res)}}, open(f"out/25_vad_ab_{stem}.json", "w"))
asyncio.run(main())
