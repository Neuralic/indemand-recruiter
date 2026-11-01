"""Phase 0b: TTS time-to-first-audio + wavs for the 10 script lines.

usage: 09_tts_ttfa.py cartesia|fish|openai [voice_name ...]

cartesia/fish: the real Pipecat TTS service (websocket) in a minimal pipeline [stamp, tts, sink]. The websocket is open
(warm) before the first line, as in production. TTFA = TTSSpeakFrame queued -> first TTSAudioRawFrame at the sink. The
service's own TTFB metric is logged alongside as a cross-check. One warm-up line is spoken first and not counted
(its time is reported separately as 'first_line_s').
openai: Realtime gpt-realtime-2.1 voice 'marin' reading the same lines (raw websocket, fresh session per line;
TTFA = response.create sent -> first response.output_audio.delta). This INCLUDES model inference, unlike a pure TTS.
Outputs: out/bakeoff/<arm>/<voice>/NN_<line>.wav and out/09_ttfa_<provider>_<voice>.json
"""

import asyncio
import base64
import json
import sys
import time

import websockets
from loguru import logger

import bakeoff_common as b
import harness as h
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    MetricsFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

ARM = {"cartesia": "B", "fish": "C", "openai": "A"}


class Stamp(FrameProcessor):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            self.sink.t_in = time.perf_counter()
        await self.push_frame(frame, direction)


class Sink(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.t_in = None
        self.reset()

    def reset(self):
        self.t_first = None
        self.pcm = bytearray()
        self.ttfb_metric = None
        self.stopped = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            if self.t_first is None:
                self.t_first = time.perf_counter()
                # No transport in this harness: emulate its BotStarted/Stopped frames. Fish sets
                # pause_frame_processing=True and otherwise waits forever for BotStoppedSpeakingFrame (deadlock seen
                # in the first Fish attempt; the real pipeline gets it from transport.output()).
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            self.pcm.extend(frame.audio)
        elif isinstance(frame, TTSStoppedFrame):
            await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            self.stopped.set()
        elif isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, TTFBMetricsData) and self.ttfb_metric is None:
                    self.ttfb_metric = d.value
        await self.push_frame(frame, direction)


async def run_pipecat(provider: str, name: str, voice_id: str) -> dict:
    out_dir = b.BAKE / ARM[provider] / name
    out_dir.mkdir(parents=True, exist_ok=True)
    tts = b.make_tts(provider, voice_id)
    sink = Sink()
    pipeline = Pipeline([Stamp(sink), tts, sink])
    worker = PipelineWorker(pipeline, params=PipelineParams(audio_out_sample_rate=24000, enable_metrics=True))
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(3.0)  # let the websocket connect (warm start)
    lag = b.LoopLag()
    rows = []
    sequence = [("warmup", "Hello, one moment please.")] + b.LINES
    for i, (key, text) in enumerate(sequence):
        sink.reset()
        await worker.queue_frames([TTSSpeakFrame(text)])
        try:
            await asyncio.wait_for(sink.stopped.wait(), timeout=45)
            timed_out = False
        except TimeoutError:
            timed_out = True
        await asyncio.sleep(0.3)
        ttfa = (sink.t_first - sink.t_in) if sink.t_first and sink.t_in else None
        secs = len(sink.pcm) / 48000
        if key != "warmup" and sink.pcm:
            h.write_wav(out_dir / f"{i:02d}_{key}.wav", bytes(sink.pcm), 24000)
        row = {"i": i, "line": key, "ttfa_s": round(ttfa, 3) if ttfa else None,
               "service_ttfb_metric_s": round(sink.ttfb_metric, 3) if sink.ttfb_metric else None,
               "audio_s": round(secs, 2), "chars": len(text), "timed_out": timed_out}
        rows.append(row)
        logger.info(f"{provider}/{name} {row}")
        await asyncio.sleep(1.0)
    lag_max = lag.stop()
    try:
        await runner.cancel()
        await asyncio.wait_for(task, 10)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"shutdown: {e!r}")
    return finish(provider, name, voice_id, rows, lag_max)


def finish(provider: str, name: str, voice_id: str, rows: list[dict], lag_max: float) -> dict:
    scored = [r["ttfa_s"] for r in rows if r["line"] != "warmup"]
    model = {"cartesia": "sonic-3.6", "fish": __import__("os").environ.get("FISH_MODEL_OVERRIDE", "s2.1-pro"),
             "openai": "gpt-realtime-2.1"}[provider]
    res = {"arm": ARM[provider], "provider": provider, "tts_model": model, "voice": name, "voice_id": voice_id,
           "first_line_s": next((r["ttfa_s"] for r in rows if r["line"] == "warmup"), None),
           "n": len([x for x in scored if x]), "ttfa_p50": b.pct(scored, 50), "ttfa_p90": b.pct(scored, 90),
           "ttfa_min": min((x for x in scored if x), default=None), "ttfa_max": max((x for x in scored if x), default=None),
           "max_loop_lag_s": lag_max, "free_ram_mb_end": b.free_ram_mb(), "rows": rows}
    (h.OUT / f"09_ttfa_{provider}_{b.slug(name)}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"{provider}/{name}: n={res['n']} p50={res['ttfa_p50']} p90={res['ttfa_p90']} first_line={res['first_line_s']} "
          f"loop_lag={lag_max} ram={res['free_ram_mb_end']}MB")
    return res


async def run_openai(voice: str = "marin") -> dict:
    url = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
    out_dir = b.BAKE / "A" / voice
    out_dir.mkdir(parents=True, exist_ok=True)
    lag = b.LoopLag()
    rows = []
    for i, (key, text) in enumerate(b.LINES, start=1):
        async with await websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {h.api_key()}"}, max_size=None) as ws:
            while json.loads(await ws.recv())["type"] != "session.created":
                pass
            await ws.send(json.dumps({"type": "session.update", "session": {
                "type": "realtime", "model": "gpt-realtime-2.1", "output_modalities": ["audio"],
                "instructions": "Read the user's text aloud EXACTLY as written, in a warm, natural, conversational "
                                "Canadian-professional tone. Say nothing else.",
                "audio": {"output": {"voice": voice}}}}))
            while json.loads(await ws.recv())["type"] != "session.updated":
                pass
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
            t0 = time.perf_counter()
            await ws.send(json.dumps({"type": "response.create"}))
            pcm, first = bytearray(), None
            while True:
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                if evt["type"] == "response.output_audio.delta":
                    first = first or (time.perf_counter() - t0)
                    pcm.extend(base64.b64decode(evt["delta"]))
                elif evt["type"] in ("response.done", "error"):
                    if evt["type"] == "error":
                        logger.error(f"error: {evt['error'].get('message', '')[:200]}")
                    break
        h.write_wav(out_dir / f"{i:02d}_{key}.wav", bytes(pcm), 24000)
        row = {"i": i, "line": key, "ttfa_s": round(first, 3) if first else None, "audio_s": round(len(pcm) / 48000, 2),
               "chars": len(text)}
        rows.append(row)
        logger.info(f"openai/{voice} {row}")
        await asyncio.sleep(1.0)
    return finish("openai", voice, voice, rows, lag.stop())


async def main() -> None:
    provider = sys.argv[1]
    only = sys.argv[2:]
    h.attach_log_file(f"09_ttfa_{provider}")
    logger.info(f"free RAM at start: {b.free_ram_mb()} MB")
    if provider == "openai":
        await run_openai(only[0] if only else "marin")
        return
    for name, vid in b.voices(provider).items():
        if only and name not in only:
            continue
        await run_pipecat(provider, name, vid)
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
