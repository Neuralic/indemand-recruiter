"""Phase 1 step 1: a live session with a HUMAN on a real mic, measuring end-to-end latency (spike code, not product).

Run:   uv run python 17_live_mic.py -t webrtc          then open http://localhost:7860/client/  (use headphones)
Config (env): TTS_VOICE=Harlow|Max   VAD_SILENCE_MS=600   LIVE_TURNS=11 (bot replies before the script ends itself)

Architecture under test = the Phase 1 design (Arm D): gpt-realtime-2.1 audio-in / TEXT-out -> Cartesia sonic-3.6.
Server-side recording only: wall-clock arrival of every mic frame + wall-clock of the bot's first audio write. Analysis
(true end of speech via Silero on the recorded mic audio, then bot-first-audio minus that) is done offline by
17b_analyze_live.py so nothing heavy runs inside the audio loop.
"""

import asyncio
import json
import re
import os
import sys
import time

from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
import bakeoff_common as b
import harness as h
from transcript_filter import PromptEchoFilter
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

# Short natural-language context (the old "Terms: A, B, C" list was echoed back verbatim on noise). Terms stay in, as prose.
TRANSCRIBE_CONTEXT = ("A recruiter is interviewing an electrical or maintenance engineer about plant reliability, APEGA and P.Eng "
                      "registration, arc flash, MCC and switchgear, PLC and SCADA systems, CMMS tools such as Maximo and SAP PM, "
                      "RCM, MTBF, lockout-tagout and Red Seal trades.")
ECHO_FILTER = PromptEchoFilter([TRANSCRIBE_CONTEXT, b.TRANSCRIBE_PROMPT])  # b.TRANSCRIBE_PROMPT = the old list prompt
VAD_THRESHOLD = float(os.environ.get("VAD_THRESHOLD", "0.5"))  # A/B on recorded mic audio (25_vad_ab.py): 0.5 + near_field = 0 missed, 0 false opens
VOICE = os.environ.get("TTS_VOICE", "Harlow")
SILENCE_MS = int(os.environ.get("VAD_SILENCE_MS", "600"))
TURNS = int(os.environ.get("LIVE_TURNS", "11"))
NAME = f"17_live_mic_{b.slug(VOICE)}_{SILENCE_MS}"

PERSONA = """You are Avery, IN DEMAND Recruitment's AI screening assistant, interviewing a candidate named David by voice for the Electrical Engineering Supervisor role in Edmonton, AB.
You sound like someone with 20 years in heavy industry: warm Canadian-professional, conversational, 1 to 3 short sentences per turn, never over-praise, never repeat a question already answered. Ask exactly one question per turn.
Cover, one at a time: why they are open to a move, a specific part of their background, a couple of competency questions (probe once if an answer is vague), then logistics (work eligibility in Canada, professional engineer registration with APEGA, notice period), then their questions, then close warmly. You know nothing about compensation, reporting line or benefits: say that is a great one for Stefanie.
Say "professional engineer" and "lockout-tagout" in full, never "P.Eng" or "LOTO". APEGA, SCADA and Maximo stay as words.
Plain spoken text only: no lists, no markdown, no emoji, no tags."""

transport_params = {"webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True)}

WATCH = (UserStartedSpeakingFrame, UserStoppedSpeakingFrame, TranscriptionFrame, LLMTextFrame, TTSAudioRawFrame,
         BotStartedSpeakingFrame, BotStoppedSpeakingFrame)


class MicTap(FrameProcessor):
    """Records every incoming mic frame: wall-clock arrival + PCM (24 kHz mono)."""

    def __init__(self, rec: dict):
        super().__init__()
        self.rec = rec

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self.rec["rate"] = frame.sample_rate
            self.rec["frames"].append((time.time(), len(frame.audio)))
            self.rec["pcm"].extend(frame.audio)
        await self.push_frame(frame, direction)


API_ERROR = re.compile(r"no credits|insufficient_quota|quota|billing|rate.?limit|429|overloaded|server_error|50[0-4]", re.I)
APOLOGY = ("I'm sorry, I'm having a technical problem on my end and can't continue this conversation. "
           "Stefanie's team will follow up with you personally. Thank you for your time.")


class LoggedRealtime(OpenAIRealtimeLLMService):
    """Records OpenAI's own VAD events (wall time + position on OpenAI's audio clock) to compare with Silero on the mic wav."""

    vad_log: list | None = None

    async def _handle_evt_speech_started(self, evt):
        if self.vad_log is not None:
            self.vad_log.append({"tap": "openai_vad", "kind": "speech_started", "t": time.time(), "audio_ms": evt.audio_start_ms})
        await super()._handle_evt_speech_started(evt)

    async def _handle_evt_speech_stopped(self, evt):
        if self.vad_log is not None:
            self.vad_log.append({"tap": "openai_vad", "kind": "speech_stopped", "t": time.time(), "audio_ms": evt.audio_end_ms})
        await super()._handle_evt_speech_stopped(evt)


class EchoGate(FrameProcessor):
    """Sits between the user aggregator and the realtime service. The realtime service pushes the finished user transcript
    UPSTREAM; strip prompt echo there and swallow turns that were only echo/noise (they never count as a candidate turn)."""

    def __init__(self, log: list):
        super().__init__()
        self.log = log

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.UPSTREAM and isinstance(frame, TranscriptionFrame):
            res = ECHO_FILTER.clean(frame.text)
            self.log.append({"tap": "user_text", "kind": "dropped" if res.dropped else "kept", "t": time.time(),
                             "raw": frame.text, "clean": res.text, "echo": res.echo_segments})
            logger.info(f"USER TRANSCRIPT {'DROPPED' if res.dropped else 'kept'}: raw={frame.text!r} clean={res.text!r}")
            if res.dropped:
                return
            frame.text = res.text
        await self.push_frame(frame, direction)


class EventTap(FrameProcessor):
    def __init__(self, name: str, log: list, on_event=None):
        super().__init__()
        self.tap_name, self.log, self.on_event, self.last = name, log, on_event, {}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, WATCH):
            k, now = type(frame).__name__, time.time()
            if not isinstance(frame, (LLMTextFrame, TTSAudioRawFrame)) or now - self.last.get(k, 0) > 1.2:
                extra = frame.text if isinstance(frame, (TranscriptionFrame, LLMTextFrame)) else None
                self.log.append({"tap": self.tap_name, "kind": k, "t": now, "text": extra})
                if self.on_event:
                    self.on_event(k)
            self.last[k] = now
        await self.push_frame(frame, direction)


def _ice_diag(pc) -> None:
    """Log candidates from both SDPs and aioice pair states (ICE-connect debugging; contains LAN IPs only)."""
    def cands(d):
        return [l.strip() for l in d.sdp.splitlines() if l.startswith("a=candidate")] if d else []

    logger.info(f"ICE diag remote candidates: {cands(pc.remoteDescription)}")
    logger.info(f"ICE diag local candidates: {cands(pc.localDescription)}")

    async def poll() -> None:
        for _ in range(12):
            await asyncio.sleep(5)
            try:
                for t in pc.getTransceivers():
                    conn = t.receiver.transport.transport._connection
                    pairs = [(p.local_candidate.host, p.remote_candidate.host, p.state.name) for p in conn._check_list]
                    logger.info(f"ICE diag state={pc.iceConnectionState} pairs={pairs} remote_cands={len(conn.remote_candidates)}")
                    break
            except Exception as e:  # noqa: BLE001
                logger.info(f"ICE diag poll error: {e!r}")

    asyncio.create_task(poll())


async def bot(runner_args: RunnerArguments):
    session = f"{NAME}_{time.strftime('%H%M%S')}"  # per-session files: a reconnect must not delete a file the last session still holds (Windows)
    log_sink = logger.add(h.OUT / f"{session}.log", level="DEBUG", format="{time:HH:mm:ss.SSS} | {level:<7} | {message}")
    logger.info(f"{session}: free RAM {b.free_ram_mb()} MB, voice={VOICE}, silence={SILENCE_MS} ms, vad_threshold={VAD_THRESHOLD}, near_field NR")
    transport = await create_transport(runner_args, transport_params)
    _ice_diag(runner_args.webrtc_connection.pc)
    events_log: list = []
    mic = {"rate": 24000, "frames": [], "pcm": bytearray()}
    bot_done = {"n": 0}
    finish_evt = asyncio.Event()

    def on_event(kind: str) -> None:
        if kind == "BotStoppedSpeakingFrame":
            bot_done["n"] += 1
            if bot_done["n"] >= TURNS:
                finish_evt.set()

    sp = events.SessionProperties(
        audio=events.AudioConfiguration(
            input=events.AudioInput(
                transcription=events.InputAudioTranscription(model="gpt-4o-transcribe", prompt=TRANSCRIBE_CONTEXT),
                noise_reduction=events.InputAudioNoiseReduction(type="near_field"),
                turn_detection=events.TurnDetection(threshold=VAD_THRESHOLD, silence_duration_ms=SILENCE_MS))))
    sp.output_modalities = ["text"]
    brain = LoggedRealtime(
        api_key=h.api_key(),
        settings=OpenAIRealtimeLLMService.Settings(model="gpt-realtime-2.1", system_instruction=PERSONA,
                                                   session_properties=sp))
    brain.vad_log = events_log
    tts = b.make_tts("cartesia", b.voices("cartesia")[VOICE])
    context = LLMContext([{"role": "user", "content": "Begin the interview now: greet David and ask if he is ready."}])
    user_agg, asst_agg = LLMContextAggregatorPair(context)
    pipeline = Pipeline([transport.input(), MicTap(mic), EventTap("in", events_log), user_agg, EchoGate(events_log), brain,
                         EventTap("llm", events_log), tts, EventTap("tts", events_log), transport.output(),
                         EventTap("out", events_log, on_event), asst_agg])
    worker = PipelineWorker(pipeline, params=PipelineParams(
        audio_in_sample_rate=24000, audio_out_sample_rate=24000, enable_metrics=True, enable_usage_metrics=True))
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)
    lag = b.LoopLag()
    bot_text: list[str] = []
    saved = {"done": False}

    def dump() -> None:
        if saved["done"]:
            return
        saved["done"] = True
        h.write_wav(h.OUT / f"{session}_mic.wav", bytes(mic["pcm"]), mic["rate"])
        res = {"voice": VOICE, "silence_ms": SILENCE_MS, "mic_rate": mic["rate"], "max_loop_lag_s": lag.stop(),
               "mic_frames": mic["frames"], "events": events_log, "bot_text": bot_text,
               "free_ram_mb_end": b.free_ram_mb()}
        (h.OUT / f"{session}.json").write_text(json.dumps(res), encoding="utf-8")
        logger.info(f"saved {session}.json ({len(events_log)} events, {len(mic['frames'])} mic frames)")

    fallback = {"done": False}

    @worker.event_handler("on_pipeline_error")
    async def _err(_w, frame):
        msg = str(getattr(frame, "error", ""))
        logger.error(f"pipeline error: {msg[:200]}")
        if API_ERROR.search(msg) and not fallback["done"]:
            fallback["done"] = True
            events_log.append({"tap": "fallback", "kind": "api_error_apology", "t": time.time(), "text": msg[:200]})
            logger.warning("API error -> speaking apology and ending the session")
            await worker.queue_frames([TTSSpeakFrame(APOLOGY), EndFrame()])  # TTS is independent of the failed LLM; EndFrame follows the speech

    @asst_agg.event_handler("on_assistant_turn_stopped")
    async def _a(_agg, m):
        if m.content:
            bot_text.append(m.content)

    @transport.event_handler("on_client_connected")
    async def _c(_t, _client):
        logger.info("client connected")
        await worker.queue_frames([LLMRunFrame()])
        if os.environ.get("SIM_API_ERROR_AFTER_S"):  # test hook: inject the quota error the real API returned
            async def sim():
                await asyncio.sleep(float(os.environ["SIM_API_ERROR_AFTER_S"]))
                await brain.push_error(error_msg="You have no credits remaining. Add credits to continue using the API (simulated).")
            asyncio.create_task(sim())

    @transport.event_handler("on_client_disconnected")
    async def _d(_t, _client):
        logger.info("client disconnected")
        dump()
        await runner.cancel()

    async def finisher() -> None:
        await finish_evt.wait()
        await asyncio.sleep(1.0)
        logger.info(f"reached {TURNS} bot replies; ending session")
        dump()
        await runner.cancel()

    fin = asyncio.create_task(finisher())
    try:
        await runner.run()
    finally:
        fin.cancel()
        dump()
        logger.remove(log_sink)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
