"""Phase 0b: end-to-end latency per arm, headless candidate over real SmallWebRTC.

usage: 13_e2e.py <arm A|B|C|D> <voice> [tts_provider for D: cartesia|fish]
  A  gpt-realtime-2.1 native voice (marin), semantic VAD                      [input -> user_agg -> realtime -> output]
  B  gpt-4o-transcribe(+domain prompt, local Silero VAD + smart-turn) -> gpt-4.1 -> Cartesia sonic-3.6
  C  same as B -> Fish (s2.1-pro-free if FISH_MODEL_OVERRIDE set; paid model returned HTTP 402)
  D  gpt-realtime-2.1 audio-in / TEXT-out (output_modalities=["text"]) -> Cartesia|Fish

Metric (identical for every arm): e2e = first bot audio energy heard by the candidate - the instant the candidate's audio
track finished playing the utterance. Component taps (VAD stop, transcript, first LLM token, first TTS audio) are logged
to show WHERE the time goes. Ten scripted turns per session; the candidate waits for the bot to finish each reply.
"""

import asyncio
import json
import os
import sys
import time

import numpy as np
from loguru import logger

import bakeoff_common as b
import harness as h
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.services.openai.stt import OpenAIRealtimeSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

ARM = sys.argv[1]
VOICE = sys.argv[2]
D_PROVIDER = sys.argv[3] if len(sys.argv) > 3 else None
PERSONA = open("bakeoff_persona.txt", encoding="utf-8").read()
TEXT_LLM = "gpt-4.1"
STT_MODEL = os.environ.get("E2E_STT", "gpt-4o-transcribe")  # diagnostic variant: gpt-realtime-whisper (streaming, no prompt)
TURN = os.environ.get("E2E_TURN", "auto")  # realtime arms only: auto | high | server  (semantic eagerness / server_vad 500 ms)
NAME = (f"13_e2e_{ARM}_{b.slug(VOICE)}" + (f"_{D_PROVIDER}" if D_PROVIDER else "")
        + (f"_{TURN}" if ARM in ("A", "D") else "") + ("_whisperstt" if os.environ.get("E2E_STT") else ""))

CANDIDATE_LINES = [
    "Yes, that sounds good. Let's get started.",
    "Honestly the plant I'm at is being sold next year, and I want to stay in heavy industry.",
    "I've spent eleven years in electrical maintenance and reliability at gas plants, the last four as a supervisor.",
    "I led our reliability program and brought unplanned downtime down from nine percent to four percent in two years.",
    "We ran monthly lockout audits, and our recordable rate dropped from two point one to point six.",
    "I supervised a team of twelve electricians and instrumentation techs, and cut overtime by thirty percent.",
    "Yes, I'm a registered P.Eng with APEGA, in good standing.",
    "I'm a Canadian citizen, so I'm legally eligible to work in Canada.",
    "I'd need about eight weeks' notice, and I'm happy to relocate to Edmonton.",
    "What's the bonus structure, and who does the role report to?",
]
WATCH = (VADUserStoppedSpeakingFrame, UserStoppedSpeakingFrame, TranscriptionFrame, LLMTextFrame, TTSAudioRawFrame,
         BotStartedSpeakingFrame)
ALWAYS = (VADUserStoppedSpeakingFrame, UserStoppedSpeakingFrame, TranscriptionFrame)


class Tap(FrameProcessor):
    """Records wall-clock times of selected frames (first of each burst) without altering the flow."""

    def __init__(self, name: str, log: list):
        super().__init__()
        self.tap_name, self.log, self.last = name, log, {}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, WATCH):
            k, now = type(frame).__name__, time.time()
            if isinstance(frame, ALWAYS) or now - self.last.get(k, 0) > 1.2:
                self.log.append((self.tap_name, k, now))
            self.last[k] = now
        await self.push_frame(frame, direction)


def trim_tail(pcm: np.ndarray, thresh: int = 400) -> np.ndarray:
    idx = np.where(np.abs(pcm.astype(np.int32)) > thresh)[0]
    return pcm[: int(idx[-1]) + 2400] if idx.size else pcm  # keep 0.1 s after the last audible sample


def build(taps: list):
    """Return (processors-after-transport-input factory, context, label)."""
    voice_id = None
    tts = None
    if ARM in ("B", "C") or (ARM == "D" and D_PROVIDER):
        provider = {"B": "cartesia", "C": "fish"}.get(ARM) or D_PROVIDER
        voice_id = b.voices(provider)[VOICE]
        tts = b.make_tts(provider, voice_id)
    if ARM in ("A", "D"):
        audio_kw = {"output": events.AudioOutput(voice=VOICE)} if ARM == "A" else {}
        td = {"auto": events.SemanticTurnDetection(), "high": events.SemanticTurnDetection(eagerness="high"),
              "server": events.TurnDetection()}[TURN]
        sp = events.SessionProperties(
            audio=events.AudioConfiguration(
                input=events.AudioInput(transcription=events.InputAudioTranscription(), turn_detection=td),
                **audio_kw),
        )
        if ARM == "D":
            sp.output_modalities = ["text"]
        brain = OpenAIRealtimeLLMService(
            api_key=h.api_key(),
            settings=OpenAIRealtimeLLMService.Settings(model="gpt-realtime-2.1", system_instruction=PERSONA,
                                                       session_properties=sp))
        stt = None
    else:
        brain = OpenAILLMService(api_key=h.api_key(),
                                 settings=OpenAILLMService.Settings(model=TEXT_LLM, system_instruction=PERSONA))
        stt = OpenAIRealtimeSTTService(
            api_key=h.api_key(),
            settings=OpenAIRealtimeSTTService.Settings(model=STT_MODEL, prompt=b.TRANSCRIBE_PROMPT))
    context = LLMContext([{"role": "user", "content": "Begin the interview now: greet Dana and ask if she is ready."}])
    return brain, stt, tts, context


async def main() -> None:
    h.attach_log_file(NAME)
    logger.info(f"{NAME}: free RAM {b.free_ram_mb()} MB")
    taps: list = []
    brain, stt, tts, context = build(taps)

    candidate = h.Candidate()
    conn = SmallWebRTCConnection()
    offer = await candidate.offer()
    await conn.initialize(sdp=offer.sdp, type=offer.type)
    transport = SmallWebRTCTransport(conn, TransportParams(audio_in_enabled=True, audio_out_enabled=True))
    cascaded = ARM in ("B", "C")
    pair = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()) if cascaded else None) \
        if cascaded else LLMContextAggregatorPair(context)
    user_agg, asst_agg = pair
    procs = [transport.input()]
    if stt:
        procs += [stt, Tap("stt", taps)]
    procs += [user_agg, brain, Tap("llm", taps)]
    if tts:
        procs += [tts, Tap("tts", taps)]
    procs += [transport.output(), asst_agg]
    worker = PipelineWorker(Pipeline(procs), params=PipelineParams(
        audio_in_sample_rate=16000 if cascaded else 24000,  # Silero VAD only accepts 8/16 kHz; the STT service upsamples to 24k
        audio_out_sample_rate=24000, enable_metrics=True, enable_usage_metrics=True))
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    connected = asyncio.Event()

    @transport.event_handler("on_client_connected")
    async def _c(_t, _c2):
        connected.set()

    bot_text: list[str] = []

    @asst_agg.event_handler("on_assistant_turn_stopped")
    async def _a(_agg, m):
        if m.content:
            bot_text.append(m.content)

    run_task = asyncio.create_task(runner.run())
    # Complete the WebRTC handshake only once the pipeline (incl. STT/TTS/Realtime websockets) is up: otherwise candidate
    # audio overtakes the StartFrame and the VAD aggregator errors ("TaskManager is not initialized").
    await asyncio.sleep(4.0)
    await candidate.accept(conn.get_answer())
    await asyncio.wait_for(connected.wait(), 30)
    await asyncio.sleep(1.5)
    lag = b.LoopLag()
    t_run = time.time()
    await worker.queue_frames([LLMRunFrame()])
    first, _ = await h.wait_bot_turn(candidate, timeout=60, quiet_s=2.0)
    greeting = round(first - t_run, 3) if first else None
    logger.info(f"greeting first audio: {greeting}s after LLMRunFrame")

    rows = []
    plan = list(CANDIDATE_LINES) + CANDIDATE_LINES[1:6]  # spare lines replace turns lost to a local stall
    valid = 0
    for i, line in enumerate(plan, start=1):
        if valid >= len(CANDIDATE_LINES):
            break
        pcm = trim_tail(await asyncio.to_thread(h.synth, line))
        n_end = len(candidate.track.audio_ended_at)
        t_turn0 = time.time()
        candidate.track.queue(pcm, lead_silence_s=0.3)
        wait_task = asyncio.create_task(h.wait_bot_turn(candidate, timeout=len(pcm) / 24000 + 60, quiet_s=1.8))
        end_wait = time.time() + len(pcm) / 24000 + 5
        while len(candidate.track.audio_ended_at) == n_end and time.time() < end_wait:
            await asyncio.sleep(0.01)
        t_end = candidate.track.audio_ended_at[-1] if len(candidate.track.audio_ended_at) > n_end else None
        first, last = await wait_task
        await asyncio.sleep(0.4)
        stalled = lag.overlaps(t_turn0, time.time())
        comp = {}
        if t_end:
            for kind in ("VADUserStoppedSpeakingFrame", "UserStoppedSpeakingFrame", "TranscriptionFrame", "LLMTextFrame",
                         "TTSAudioRawFrame"):
                ts = [t for _, k, t in taps if k == kind and t >= t_end]
                comp[kind] = round(min(ts) - t_end, 3) if ts else None
        e2e = round(first - t_end, 3) if first and t_end else None
        ok = e2e is not None and e2e > 0 and not stalled
        valid += ok
        row = {"turn": i, "line": line, "e2e_s": e2e, "valid": ok, "local_stall": stalled,
               "bot_speech_s": round(last - first, 2) if first and last else None, "components_s": comp,
               "bot_reply": bot_text[-1][:160] if bot_text else None}
        rows.append(row)
        logger.info(f"turn {i}: e2e={e2e} valid={ok} stall={stalled} comp={comp}")
        if lag.events and lag.events[-1][2] > 20:  # transport/peer is dead after a stall this long; stop and let the driver retry
            logger.error("aborting session: long local stall")
            break
    lag_max = lag.stop()

    await runner.cancel()
    try:
        await asyncio.wait_for(run_task, 10)
    except Exception:  # noqa: BLE001
        run_task.cancel()
    wav_dir = b.BAKE / "e2e_audio"
    wav_dir.mkdir(exist_ok=True)
    h.write_wav(wav_dir / f"{NAME}.wav", bytes(candidate.bot.pcm), candidate.bot.rate)  # whole bot side of the call
    await candidate.close()

    vals = [r["e2e_s"] for r in rows if r["valid"]]
    res = {"arm": ARM, "voice": VOICE, "d_provider": D_PROVIDER, "turn_detection": TURN if ARM in ("A", "D") else "silero+smart-turn-v3", "text_llm": TEXT_LLM if cascaded else None,
           "tts_model": (getattr(tts._settings, "model", None) if tts else None),
           "greeting_first_audio_s": greeting, "n_valid": len(vals), "n_turns_run": len(rows), "stalls": [round(x[2], 1) for x in lag.events], "e2e_p50": b.pct(vals, 50), "e2e_p90": b.pct(vals, 90),
           "e2e_min": min(vals, default=None), "e2e_max": max(vals, default=None), "max_loop_lag_s": lag_max,
           "free_ram_mb_end": b.free_ram_mb(), "rows": rows}
    (h.OUT / f"{NAME}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"{NAME}: n_valid={res['n_valid']}/{res['n_turns_run']} e2e p50={res['e2e_p50']} p90={res['e2e_p90']} greeting={greeting} "
          f"lag={lag_max} ram={res['free_ram_mb_end']}MB")


if __name__ == "__main__":
    asyncio.run(main())
