"""Phase 1 live interview harness (WebRTC, real mic): role #5905 (PLACEHOLDER config), Arm D.

Run:  PYTHONUTF8=1 env -u PIPECAT_ICE_SERVERS .venv312/Scripts/python.exe 26_live_interview.py -t webrtc
      then open http://localhost:7860/client/ (headphones).
Env:  TTS_VOICE=Harlow|Max  VAD_SILENCE_MS=600  VAD_THRESHOLD=0.5  CANDIDATE_NAME=David  EVAL_MODEL=gpt-5.6-terra  EVAL_EFFORT=low
      TIME_SCALE / SILENCE_SCALE (dev: shrink the 13/16-minute and 8/20/45-second timers)  MAX_BOT_REPLIES=80 (safety cap)
Output per session: out/26_interview_<voice>_<silence>_<HHMMSS>.{log,json}, _mic.wav (candidate), _bot.wav (Avery, as played, same
timeline) and _mix.wav (both, to listen back). json = events + interview summary.
"""

import asyncio
import json
import os
import sys
import time

from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
import bakeoff_common as b
import harness as h
from evaluator import EFFORT, MODEL, Evaluator
from interview import Interview
from persona import ACK_PERSONA, APOLOGY, KICKOFF, OLD_LIST_PROMPT, PREVIOUS_CONTEXT, PROMPT_VERSION, TRANSCRIBE_CONTEXT
from pipeline_parts import (API_ERROR, BotTap, Director, EchoGate, EndTrace, EventTap, GreetingGate, GreetingTap, InstrumentedRealtime, MicGate, MicTap, mix_pcm,
                            render_bot_audio)
from role_config import load_role
from transcript_filter import PromptEchoFilter

from tts_failover import CartesiaProvider, FailoverCore, FishProvider, StubProvider, make_speech_output
from warm_pool import WarmPool

from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.run import app
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

VAD_THRESHOLD = float(os.environ.get("VAD_THRESHOLD", "0.5"))
VOICE = os.environ.get("TTS_VOICE", "Harlow")
# Real (credit-spending) voices are OPT-IN: REAL_VOICE=1 on the server AND a client that does not identify itself as a simulation.
# Everything else (automated runs, the headless simulated candidate, a forgotten flag) gets the stub provider, which spends nothing.
REAL_VOICE = os.environ.get("REAL_VOICE", "0") == "1"
# Fish fallback voice. Laura, not Abby/Ethan: Harlow is a feminine, Canadian "Warm Support" voice (friendly yet professional); of the
# Fish shortlist Laura's tags are female, middle-aged, conversational, warm, calm, professional, clear (the closest match), Abby is an
# "American accent" audiobook narrator, Ethan is male.
FISH_VOICE = os.environ.get("FISH_VOICE", "Laura")
STUB_FAULTS = os.environ.get("STUB_FAULTS", "")  # e.g. "402@14" or "stall@14": inject a Cartesia-stub failure at its Nth sentence
STUB_FISH_FAULTS = os.environ.get("STUB_FISH_FAULTS", "")
SILENCE_MS = int(os.environ.get("VAD_SILENCE_MS", "600"))
CANDIDATE = os.environ.get("CANDIDATE_NAME", "David")
TIME_SCALE = float(os.environ.get("TIME_SCALE", "1"))
SILENCE_SCALE = float(os.environ.get("SILENCE_SCALE", "1"))
MAX_REPLIES = int(os.environ.get("MAX_BOT_REPLIES", "80"))
NAME = f"26_interview_{b.slug(VOICE)}_{SILENCE_MS}"

def keep_awake() -> None:
    """Ask Windows not to idle into Modern Standby while a call is running (it froze the whole process for 3 minutes in test
    run 182848). Per-process request that ends when this process exits; no system setting is changed."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED


keep_awake()
def _make_pool() -> WarmPool:
    t = b.make_tts("cartesia", b.voices("cartesia")[VOICE])  # only to read the connection parameters; never started
    return WarmPool(f"wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1", {"Authorization": f"Bearer {h.api_key()}"},
                    t._url, {"X-API-Key": t._api_key, "Cartesia-Version": t._cartesia_version, **t._extra_headers})


POOL = _make_pool() if os.environ.get("PREWARM", "1") == "1" else None


@app.on_event("startup")
async def _start_pool() -> None:
    if POOL:
        POOL.start()
        logger.info("pre-warm enabled: keeping one OpenAI Realtime + one Cartesia websocket open for the next session")


transport_params = {"webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True)}


async def bot(runner_args: RunnerArguments):
    session = f"{NAME}_{time.strftime('%H%M%S')}"
    log_sink = logger.add(h.OUT / f"{session}.log", level="DEBUG", format="{time:HH:mm:ss.SSS} | {level:<7} | {message}")
    role = load_role("5905")
    logger.info(f"{session}: voice={VOICE} silence={SILENCE_MS}ms vad={VAD_THRESHOLD} eval={MODEL}/{EFFORT} candidate={CANDIDATE} "
                f"free RAM {b.free_ram_mb()} MB")
    logger.warning(f"role {role.id} still has PLACEHOLDER blocks: {role.placeholders}")
    transport = await create_transport(runner_args, transport_params)
    events_log: list = []
    mic = {"rate": 24000, "frames": [], "pcm": bytearray()}
    worker_ref: dict = {}
    bot_rec: dict = {}

    interview = Interview(role, CANDIDATE, time_scale=TIME_SCALE)
    evaluator = Evaluator(role)
    sp = events.SessionProperties(
        audio=events.AudioConfiguration(
            input=events.AudioInput(
                transcription=events.InputAudioTranscription(model="gpt-4o-transcribe", prompt=TRANSCRIBE_CONTEXT),
                noise_reduction=events.InputAudioNoiseReduction(type="near_field"),
                turn_detection=events.TurnDetection(threshold=VAD_THRESHOLD, silence_duration_ms=SILENCE_MS))))
    sp.output_modalities = ["text"]
    brain = InstrumentedRealtime(
        api_key=h.api_key(),
        settings=OpenAIRealtimeLLMService.Settings(model="gpt-realtime-2.1", system_instruction=ACK_PERSONA, session_properties=sp))
    brain.vad_log = events_log
    sim = isinstance(getattr(runner_args, "body", None), dict) and bool(runner_args.body.get("sim"))
    use_real = REAL_VOICE and not sim
    body = runner_args.body if sim else {}
    faults, fish_faults = body.get("faults") or STUB_FAULTS, body.get("fish_faults") or STUB_FISH_FAULTS  # per-session fault injection (sims only)
    warm = POOL.take() if POOL else None
    if warm:
        brain.warm_ws = warm.openai_ws  # the realtime service skips its own connect when a socket is present
    if use_real:
        cart = CartesiaProvider(b.make_tts("cartesia", b.voices("cartesia")[VOICE]), warm_ws=warm.cartesia_ws if warm else None)
        providers = [cart, FishProvider(os.environ["FISH_API_KEY"], b.voices("fish")[FISH_VOICE],
                                        model=os.environ.get("FISH_MODEL_OVERRIDE", "s2.1-pro-free"))]
        tts_mode = f"REAL voice: cartesia/{VOICE} -> fish s2.1-pro-free/{FISH_VOICE}"
    else:
        if warm:
            asyncio.create_task(warm.close_cartesia())  # the warm Cartesia socket is not needed in stub mode
        providers = [StubProvider("cartesia-stub", ttfb=0.45, faults=faults), StubProvider("fish-stub", ttfb=1.2, faults=fish_faults)]
        tts_mode = f"STUB voice (no credits; {'simulated client' if sim else 'REAL_VOICE not set'}) faults={faults or '-'}/{fish_faults or '-'}"
    logger.warning(f"TTS MODE: {tts_mode}")
    core = FailoverCore(providers, log=events_log)
    logger.info(f"pre-warm: {'WARM sockets adopted (age %.0fs)' % (time.time() - warm.born) if warm else 'COLD start (no warm set ready)'}")
    events_log.append({"tap": "warm", "kind": "warm" if warm else "cold", "t": time.time()})
    director = Director(interview, evaluator, brain, events_log, worker_ref, silence_scale=SILENCE_SCALE)
    speech = make_speech_output(core, director.voice_failed)
    gate = GreetingGate(on_open=director.greeting_played)
    echo = EchoGate(events_log, PromptEchoFilter([TRANSCRIBE_CONTEXT, PREVIOUS_CONTEXT, OLD_LIST_PROMPT, b.TRANSCRIBE_PROMPT]), director.on_user_turn,
                     audio_of=brain.audio_seconds)
    context = LLMContext([{"role": "user", "content": KICKOFF}])
    user_agg, asst_agg = LLMContextAggregatorPair(context)
    n_out = {"n": 0}
    finish_evt = asyncio.Event()

    def on_out(kind: str) -> None:
        if kind == "BotStoppedSpeakingFrame":
            n_out["n"] += 1
            if n_out["n"] >= MAX_REPLIES:
                finish_evt.set()

    chain = [transport.input(), MicTap(mic), MicGate(gate), EventTap("in", events_log), user_agg, echo, brain,
             EventTap("llm", events_log), director, speech, GreetingTap(gate), EventTap("tts", events_log), BotTap(bot_rec), transport.output(),
             EventTap("out", events_log, on_out), asst_agg]
    if os.environ.get("TRACE_END"):  # debug: log where an EndFrame stops
        chain = [x for p in chain for x in (p, EndTrace(f"after {type(p).__name__}"))]
    pipeline = Pipeline(chain)
    if os.environ.get("TRACE_END"):
        stages = list(pipeline.processors) if hasattr(pipeline, "processors") else []
        logger.info(f"TRACE_END on; stages={len(stages)}")
    worker = PipelineWorker(pipeline, params=PipelineParams(
        audio_in_sample_rate=24000, audio_out_sample_rate=24000, enable_metrics=True, enable_usage_metrics=True))
    worker_ref["worker"] = worker
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)
    lag = b.LoopLag()
    bot_text: list[str] = []
    saved = {"done": False}

    def dump() -> None:
        if saved["done"]:
            return
        saved["done"] = True
        director.shutdown()
        h.write_wav(h.OUT / f"{session}_mic.wav", bytes(mic["pcm"]), mic["rate"])
        try:  # the bot's side, on the mic's timeline, so the whole call can be listened back to (stub mode: a faint hum)
            t0 = mic["frames"][0][0] if mic["frames"] else time.time()
            bot_pcm = render_bot_audio(bot_rec, t0, len(mic["pcm"]) / 2 / mic["rate"], mic["rate"])
            h.write_wav(h.OUT / f"{session}_bot.wav", bot_pcm, mic["rate"])
            h.write_wav(h.OUT / f"{session}_mix.wav", mix_pcm(bytes(mic["pcm"]), bot_pcm), mic["rate"])
        except Exception as e:  # noqa: BLE001 - never lose the session json over a wav
            logger.warning(f"could not write the bot/mix wav: {e!r}")
        res = {"voice": VOICE, "silence_ms": SILENCE_MS, "mic_rate": mic["rate"], "max_loop_lag_s": lag.stop(),
               "mic_frames": mic["frames"], "events": events_log, "bot_text": bot_text, "free_ram_mb_end": b.free_ram_mb(),
               "interview": interview.summary(), "transcript": interview.history,
               "config": {"realtime_model": "gpt-realtime-2.1", "tts": tts_mode, "evaluator": f"{MODEL}/{EFFORT}",
                          "prompt_version": PROMPT_VERSION, "vad_threshold": VAD_THRESHOLD, "silence_ms": SILENCE_MS,
                          "role": role.id, "role_placeholders": role.placeholders}}
        (h.OUT / f"{session}.json").write_text(json.dumps(res), encoding="utf-8")
        logger.info(f"saved {session}.json ({len(events_log)} events, {len(mic['frames'])} mic frames)")

    async def finish_session() -> None:
        logger.info("interview over; finalising session")
        dump()
        await runner.cancel()

    worker_ref["finish"] = finish_session

    @worker.event_handler("on_pipeline_error")
    async def _err(_w, frame):
        msg = str(getattr(frame, "error", ""))
        logger.error(f"pipeline error: {msg[:200]}")
        if API_ERROR.search(msg):
            await director.fatal_apology(msg)

    @asst_agg.event_handler("on_assistant_turn_stopped")
    async def _a(_agg, m):
        if m.content:
            bot_text.append(m.content)

    @transport.event_handler("on_client_connected")
    async def _c(_t, _client):
        logger.info("client connected")
        await worker.queue_frames([LLMRunFrame()])  # session setup only; the model's first (kickoff) response is muted by the director
        await director.start()
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
        logger.info(f"reached {MAX_REPLIES} bot replies; ending session")
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
