"""Shared Phase 0 harness.

A real SmallWebRTC server connection (Pipecat) + a headless aiortc "candidate" in the same
process. The candidate speaks TTS-synthesised audio over WebRTC and records what the bot says
back. This exercises the actual transport, the actual OpenAIRealtimeLLMService, and the actual
OpenAI Realtime API end to end - the only thing replaced is the human mouth/microphone.

Caveat (carried into the report): synthetic TTS speech is cleaner than a real phone/laptop mic,
so transcription accuracy measured here is an upper bound.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import wave
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import av
import httpx
import numpy as np
import soxr
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from dotenv import load_dotenv
from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

load_dotenv(override=True)

HERE = Path(__file__).parent
OUT = HERE / "out"
AUDIO_CACHE = OUT / "audio"
OUT.mkdir(exist_ok=True)
AUDIO_CACHE.mkdir(exist_ok=True)


def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    assert key, "OPENAI_API_KEY missing from .env"
    return key


# --------------------------------------------------------------------------------------
# Candidate speech synthesis (OpenAI TTS -> 24 kHz mono s16 PCM), cached on disk.
# --------------------------------------------------------------------------------------
def synth(text: str, voice: str = "onyx", model: str = "gpt-4o-mini-tts", instructions: str | None = None) -> np.ndarray:
    """Return 24 kHz mono int16 samples for `text`."""
    tag = hashlib.sha1(f"{model}|{voice}|{instructions}|{text}".encode()).hexdigest()[:16]
    path = AUDIO_CACHE / f"{tag}.pcm"
    if not path.exists():
        body = {"model": model, "voice": voice, "input": text, "response_format": "pcm"}
        if instructions:
            body["instructions"] = instructions
        for attempt in range(4):
            try:
                r = httpx.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {api_key()}"},
                    json=body,
                    timeout=120,
                )
                r.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        path.write_bytes(r.content)
    return np.frombuffer(path.read_bytes(), dtype=np.int16)


def write_wav(path: Path, pcm: bytes | np.ndarray, rate: int, channels: int = 1) -> None:
    if isinstance(pcm, np.ndarray):
        pcm = pcm.astype(np.int16).tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# --------------------------------------------------------------------------------------
# The headless candidate (aiortc client)
# --------------------------------------------------------------------------------------
class QueueAudioTrack(MediaStreamTrack):
    """Paced 48 kHz mono s16 track: plays queued PCM, silence otherwise."""

    kind = "audio"
    RATE = 48000
    SAMPLES = 960  # 20 ms

    def __init__(self) -> None:
        super().__init__()
        self._buf = bytearray()
        self._start: float | None = None
        self._n = 0
        self._queued = 0  # total bytes ever queued
        self._played = 0  # total bytes ever emitted (incl. silence-free audio only)
        self.audio_started_at: list[float] = []  # wall time each utterance began playing
        self.audio_ended_at: list[float] = []
        self._marks: list[tuple[int, str]] = []  # (absolute byte offset, kind)

    def queue(self, pcm24k: np.ndarray, lead_silence_s: float = 0.0) -> None:
        up = soxr.resample(pcm24k.astype(np.int16), 24000, self.RATE).astype(np.int16).tobytes()
        if lead_silence_s:
            sil = b"\x00" * (int(self.RATE * lead_silence_s) * 2)
            self._buf.extend(sil)
            self._queued += len(sil)
        self._marks.append((self._queued, "start"))
        self._buf.extend(up)
        self._queued += len(up)
        self._marks.append((self._queued, "end"))

    @property
    def idle(self) -> bool:
        return len(self._buf) == 0

    async def recv(self):
        if self._start is None:
            self._start = time.time()
        self._n += 1
        wait = self._start + (self._n * self.SAMPLES) / self.RATE - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        nbytes = self.SAMPLES * 2
        chunk = bytes(self._buf[:nbytes])
        del self._buf[:nbytes]
        self._played += len(chunk)
        while self._marks and self._marks[0][0] <= self._played:
            _, kind = self._marks.pop(0)
            (self.audio_started_at if kind == "start" else self.audio_ended_at).append(time.time())
        chunk = chunk.ljust(nbytes, b"\x00")
        frame = av.AudioFrame(format="s16", layout="mono", samples=self.SAMPLES)
        frame.planes[0].update(chunk)
        frame.sample_rate = self.RATE
        frame.pts = (self._n - 1) * self.SAMPLES
        frame.time_base = Fraction(1, self.RATE)
        return frame


@dataclass
class BotAudio:
    """What the candidate heard back, with wall-clock energy envelope."""

    pcm: bytearray = field(default_factory=bytearray)
    rate: int = 48000
    channels: int = 1
    env: list[tuple[float, float]] = field(default_factory=list)  # (wall_time, rms)
    first_frame_at: float | None = None

    def speaking_segments(self, thresh: float = 300.0, min_gap_s: float = 0.6) -> list[tuple[float, float]]:
        segs: list[list[float]] = []
        for t, rms in self.env:
            if rms >= thresh:
                if segs and t - segs[-1][1] <= min_gap_s:
                    segs[-1][1] = t
                else:
                    segs.append([t, t])
        return [(a, b) for a, b in segs]


class Candidate:
    def __init__(self) -> None:
        self.pc = RTCPeerConnection()
        self.track = QueueAudioTrack()
        self.bot = BotAudio()
        self.pc.addTrack(self.track)
        self.channel = self.pc.createDataChannel("pipecat")  # server expects an RTVI data channel
        self._recv_task: asyncio.Task | None = None

        @self.pc.on("track")
        def _on_track(track):
            if track.kind == "audio":
                self._recv_task = asyncio.create_task(self._drain(track))

    async def _drain(self, track) -> None:
        try:
            while True:
                frame = await track.recv()
                arr = frame.to_ndarray()
                self.bot.rate = frame.sample_rate
                ch = len(frame.layout.channels)
                self.bot.channels = ch
                if self.bot.first_frame_at is None:
                    self.bot.first_frame_at = time.time()
                mono = arr.reshape(-1, ch)[:, 0] if arr.ndim == 2 and arr.shape[0] == 1 else arr.reshape(-1)
                # packed s16 stereo comes back as shape (1, n*ch); take first channel for the envelope
                if ch == 2 and arr.ndim == 2 and arr.shape[0] == 1:
                    mono = arr.reshape(-1, 2)[:, 0]
                rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2))) if mono.size else 0.0
                self.bot.env.append((time.time(), rms))
                self.bot.pcm.extend(arr.astype(np.int16).tobytes())
        except Exception:
            pass

    async def offer(self) -> RTCSessionDescription:
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return self.pc.localDescription

    async def accept(self, answer: dict) -> None:
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    def say(self, text: str, voice: str = "onyx", lead_silence_s: float = 0.0, degraded: bool = False, **kw) -> float:
        """Queue speech. NOTE: blocks the event loop on a cache miss - pre-synth with presynth() or use asay()."""
        pcm = synth(text, voice=voice, **kw)
        if degraded:
            pcm = degrade(pcm)
        self.track.queue(pcm, lead_silence_s=lead_silence_s)
        return len(pcm) / 24000.0

    async def asay(self, text: str, voice: str = "onyx", lead_silence_s: float = 0.0, degraded: bool = False, **kw) -> float:
        """Non-blocking say(): synthesis runs in a worker thread so the Pipecat loop / OpenAI keepalive never stall."""
        t0 = time.time()
        pcm = await asyncio.to_thread(synth, text, voice, **kw)
        if time.time() - t0 > 3:
            logger.warning(f"slow TTS synth: {time.time() - t0:.1f}s")
        if degraded:
            pcm = degrade(pcm)
        self.track.queue(pcm, lead_silence_s=lead_silence_s)
        return len(pcm) / 24000.0

    async def close(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
        await self.pc.close()


# --------------------------------------------------------------------------------------
# Bot session runner
# --------------------------------------------------------------------------------------
@dataclass
class Session:
    llm: OpenAIRealtimeLLMService
    context: LLMContext
    worker: PipelineWorker
    candidate: Candidate
    transport: SmallWebRTCTransport
    connected: asyncio.Event
    t0: float
    aggregators: object = None  # the LLMContextAggregatorPair (Flows needs it)


async def open_session(
    llm: OpenAIRealtimeLLMService,
    context: LLMContext,
    user_aggregator_handlers=None,
    assistant_handlers=None,
    observers=None,
) -> tuple[Session, asyncio.Task, WorkerRunner]:
    candidate = Candidate()
    server_conn = SmallWebRTCConnection()
    offer = await candidate.offer()
    await server_conn.initialize(sdp=offer.sdp, type=offer.type)
    await candidate.accept(server_conn.get_answer())

    transport = SmallWebRTCTransport(
        server_conn,
        TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    pair = LLMContextAggregatorPair(context)
    user_agg, asst_agg = pair
    for ev, fn in (user_aggregator_handlers or {}).items():
        user_agg.event_handler(ev)(fn)
    for ev, fn in (assistant_handlers or {}).items():
        asst_agg.event_handler(ev)(fn)

    pipeline = Pipeline([transport.input(), user_agg, llm, transport.output(), asst_agg])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=24000,
            audio_out_sample_rate=24000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=observers or [],
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    connected = asyncio.Event()

    @transport.event_handler("on_client_connected")
    async def _on_connected(_t, _c):
        logger.info("transport: client connected")
        connected.set()

    @transport.event_handler("on_client_disconnected")
    async def _on_disc(_t, _c):
        logger.info("transport: client disconnected")

    run_task = asyncio.create_task(runner.run())
    sess = Session(llm, context, worker, candidate, transport, connected, time.time(), aggregators=pair)
    return sess, run_task, runner


async def close_session(sess: Session, run_task: asyncio.Task, runner: WorkerRunner) -> None:
    try:
        await runner.cancel()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"runner.cancel: {e!r}")
    try:
        await asyncio.wait_for(run_task, timeout=10)
    except Exception:
        run_task.cancel()
    await sess.candidate.close()


# --------------------------------------------------------------------------------------
# Turn helpers
# --------------------------------------------------------------------------------------
async def wait_bot_turn(cand: Candidate, quiet_s: float = 1.6, timeout: float = 40.0, thresh: float = 300.0,
                        require_speech: bool = True) -> tuple[float | None, float | None]:
    """Wait until the bot has spoken and then been quiet for `quiet_s`.

    Returns (first_energy_wall_time, last_energy_wall_time) of the NEW speech, or (None, None) on timeout.
    """
    start_idx = len(cand.bot.env)
    t_end = time.time() + timeout
    first = last = None
    while time.time() < t_end:
        await asyncio.sleep(0.05)
        for t, rms in cand.bot.env[start_idx:]:
            if rms >= thresh:
                first = first or t
                last = t
        start_idx = len(cand.bot.env)
        if first is not None and time.time() - last >= quiet_s:
            return first, last
        if first is None and not require_speech:
            return None, None
    return first, last


def attach_log_file(name: str) -> Path:
    path = OUT / f"{name}.log"
    if path.exists():
        path.unlink()
    logger.add(path, level="DEBUG", format="{time:HH:mm:ss.SSS} | {level:<7} | {message}", enqueue=False)
    return path


def degrade(pcm: np.ndarray, snr_db: float = 10.0, seed: int = 0) -> np.ndarray:
    """Make clean TTS sound like a worse mic: add white noise at `snr_db`, then band-limit via an 8 kHz round trip."""
    rng = np.random.default_rng(seed)
    x = pcm.astype(np.float64)
    sig_p = np.mean(x ** 2) + 1e-9
    noise = rng.normal(0, np.sqrt(sig_p / (10 ** (snr_db / 10))), size=x.shape)
    y = soxr.resample((x + noise).astype(np.float32), 24000, 8000)
    y = soxr.resample(y, 8000, 24000)
    return np.clip(y, -32768, 32767).astype(np.int16)
