"""Phase 0b: does the TTS websocket drop / need reconnect logic over a ~10 minute session?

usage: 15_soak.py cartesia|fish [voice]
The real Pipecat TTS service holds one websocket for the whole session. Schedule (seconds from start):
  active: a ~9 s bot line every 40 s for 4 minutes (t=0..200), then a 320 s IDLE gap (longer than Cartesia's documented
  5-minute inactivity timeout - see cartesia/tts.py "times out after 5 minutes of innactivity"), then lines at
  t=520, 560, 600. Each line records: TTFA, audio seconds, errors. Connection events (on_connected / on_disconnected /
  on_connection_error) and any service ErrorFrame are timestamped. PipelineWorker idle cancel is disabled, because
  with no transport here there is no audio to keep it alive (a real call has a constant input stream).
Outputs: out/15_soak_<provider>.json (+ .log)
"""

import asyncio
import importlib.util
import json
import sys
import time

from loguru import logger

import bakeoff_common as b
import harness as h
from pipecat.frames.frames import ErrorFrame, Frame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

spec = importlib.util.spec_from_file_location("t09", "09_tts_ttfa.py")
t09 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t09)

PROVIDER = sys.argv[1]
VOICE = sys.argv[2] if len(sys.argv) > 2 else next(iter(b.voices(PROVIDER)))
SCHEDULE = [0, 40, 80, 120, 160, 200, 520, 560, 600]
LINE = ("Thanks, that's helpful. Can you give me one specific example of how you handled that, "
        "what you personally did, and what the result was?")


class ErrTap(FrameProcessor):
    def __init__(self, events: list, t0: float):
        super().__init__()
        self.events, self.t0 = events, t0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame):
            self.events.append({"t": round(time.monotonic() - self.t0, 1), "event": "ErrorFrame",
                                "detail": str(frame.error)[:200]})
        await self.push_frame(frame, direction)


async def main() -> None:
    h.attach_log_file(f"15_soak_{PROVIDER}")
    logger.info(f"free RAM {b.free_ram_mb()} MB; voice {VOICE}")
    t0 = time.monotonic()
    events: list[dict] = []
    tts = b.make_tts(PROVIDER, b.voices(PROVIDER)[VOICE])
    for ev in ("on_connected", "on_disconnected", "on_connection_error"):
        def make(name):
            async def handler(_svc, *args):
                events.append({"t": round(time.monotonic() - t0, 1), "event": name, "detail": str(args)[:120]})
                logger.warning(f"EVENT {name} at t={events[-1]['t']}s {args}")
            return handler
        tts.event_handler(ev)(make(ev))
    sink = t09.Sink()
    worker = PipelineWorker(Pipeline([t09.Stamp(sink), tts, ErrTap(events, t0), sink]),
                            params=PipelineParams(audio_out_sample_rate=24000, enable_metrics=True),
                            cancel_on_idle_timeout=False, idle_timeout_secs=None)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    task = asyncio.create_task(runner.run())
    lag = b.LoopLag()
    t0 = time.monotonic()
    await asyncio.sleep(2.0)
    rows = []
    for k, at in enumerate(SCHEDULE):
        await asyncio.sleep(max(0, at - (time.monotonic() - t0)))
        sink.reset()
        t_req = time.monotonic() - t0
        await worker.queue_frames([TTSSpeakFrame(LINE)])
        try:
            await asyncio.wait_for(sink.stopped.wait(), 45)
            timed_out = False
        except TimeoutError:
            timed_out = True
        await asyncio.sleep(0.3)
        ttfa = (sink.t_first - sink.t_in) if sink.t_first and sink.t_in else None
        row = {"line": k, "scheduled_t": at, "requested_t": round(t_req, 1),
               "idle_before_s": round(at - SCHEDULE[k - 1], 0) if k else None, "ttfa_s": round(ttfa, 3) if ttfa else None,
               "audio_s": round(len(sink.pcm) / 48000, 2), "got_audio": bool(sink.pcm), "timed_out": timed_out}
        rows.append(row)
        logger.info(f"soak line {row}")
    await asyncio.sleep(2)
    lag_max = lag.stop()
    total_s = round(time.monotonic() - t0, 0)
    await runner.cancel()
    try:
        await asyncio.wait_for(task, 10)
    except Exception:  # noqa: BLE001
        pass
    log_text = open(h.OUT / f"15_soak_{PROVIDER}.log", encoding="utf-8").read().lower()
    res = {"provider": PROVIDER, "voice": VOICE, "session_s": total_s, "lines_ok": sum(r["got_audio"] for r in rows),
           "lines_total": len(rows), "events": events, "reconnect_log_lines": log_text.count("reconnect"),
           "max_loop_lag_s": round(lag_max, 2), "stall_events": [round(x[2], 1) for x in lag.events], "rows": rows,
           "tts_model": getattr(tts._settings, "model", None)}
    (h.OUT / f"15_soak_{PROVIDER}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"15_soak {PROVIDER}: session={total_s}s lines_ok={res['lines_ok']}/{res['lines_total']} events={len(events)} "
          f"reconnect_log_lines={res['reconnect_log_lines']} lag={res['max_loop_lag_s']}")


if __name__ == "__main__":
    asyncio.run(main())
