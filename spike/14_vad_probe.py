"""Phase 0b: how long after the candidate stops does OpenAI's server declare the turn over (no Pipecat in the loop)?

usage: 14_vad_probe.py auto|high|server|server300
Streams each candidate clip in real time (20 ms chunks) followed by digital silence, and measures
  delta = (arrival of input_audio_buffer.speech_stopped) - (moment the last speech sample was sent).
create_response=false so no model output is involved. server = server_vad silence_duration_ms 500; server300 = 300.
"""

import asyncio
import base64
import importlib.util
import json
import sys
import time

import numpy as np
import websockets
from loguru import logger

import bakeoff_common as b
import harness as h

MODE = sys.argv[1]
spec = importlib.util.spec_from_file_location("e", "13_e2e.py")
sys.argv = ["x", "A", "marin"]
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)
sys.argv = ["x", MODE]

TD = {
    "auto": {"type": "semantic_vad", "create_response": False},
    "high": {"type": "semantic_vad", "eagerness": "high", "create_response": False},
    "server": {"type": "server_vad", "silence_duration_ms": 500, "create_response": False},
    "server300": {"type": "server_vad", "silence_duration_ms": 300, "create_response": False},
}[MODE]


async def main() -> None:
    h.attach_log_file(f"14_vad_probe_{MODE}")
    rows = []
    async with websockets.connect("wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
                                  additional_headers={"Authorization": f"Bearer {h.api_key()}"}, max_size=None) as ws:
        while json.loads(await ws.recv())["type"] != "session.created":
            pass
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "model": "gpt-realtime-2.1", "output_modalities": ["audio"],
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": TD}}}}))
        stopped = asyncio.Queue()

        async def reader():
            async for raw in ws:
                evt = json.loads(raw)
                if evt["type"] == "input_audio_buffer.speech_stopped":
                    stopped.put_nowait(time.perf_counter())
                elif evt["type"] == "error":
                    logger.error(evt["error"].get("message", "")[:200])

        rtask = asyncio.create_task(reader())
        await asyncio.sleep(1.0)
        lag = b.LoopLag()

        async def stream(pcm: np.ndarray) -> float:
            """Send in real time; return perf_counter when the final chunk was sent."""
            t = time.perf_counter()
            for i in range(0, len(pcm), 480):
                chunk = pcm[i:i + 480].astype(np.int16)
                await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(chunk.tobytes()).decode()}))
                t += 0.02
                await asyncio.sleep(max(0, t - time.perf_counter()))
            return time.perf_counter()

        for i, line in enumerate(e.CANDIDATE_LINES, start=1):
            while not stopped.empty():
                stopped.get_nowait()
            pcm = e.trim_tail(await asyncio.to_thread(h.synth, line))
            t_end = await stream(np.concatenate([np.zeros(4800, np.int16), pcm]))  # 0.2 s lead-in
            sil = asyncio.create_task(stream(np.zeros(24000 * 10, np.int16)))  # up to 10 s of silence, paced
            try:
                t_stop = await asyncio.wait_for(stopped.get(), 12)
                d = round(t_stop - t_end, 3)
            except TimeoutError:
                d = None
            sil.cancel()
            rows.append({"turn": i, "speech_s": round(len(pcm) / 24000, 2), "stop_after_end_s": d})
            logger.info(rows[-1])
            await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
            await asyncio.sleep(1.5)
        lag_max = lag.stop()
        rtask.cancel()
    vals = [r["stop_after_end_s"] for r in rows if r["stop_after_end_s"] is not None]
    res = {"mode": MODE, "turn_detection": TD, "n": len(vals), "p50": b.pct(vals, 50), "p90": b.pct(vals, 90),
           "min": min(vals, default=None), "max": max(vals, default=None), "max_loop_lag_s": lag_max, "rows": rows}
    (h.OUT / f"14_vad_probe_{MODE}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"14_vad_probe {MODE}: n={res['n']} p50={res['p50']} p90={res['p90']} min={res['min']} max={res['max']} lag={lag_max}")


if __name__ == "__main__":
    asyncio.run(main())
