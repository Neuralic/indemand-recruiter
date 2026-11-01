"""Pipecat processors for the Phase 1 live pipeline (Arm D: gpt-realtime-2.1 audio-in/text-out -> Cartesia).

  transport.input -> MicTap -> user_agg -> EchoGate -> brain -> Director -> tts -> transport.output -> asst_agg

Division of labour (CLAUDE.md §14):
  * brain (realtime model)  hears the audio and says ONE short acknowledgement per candidate turn (persona.ACK_PERSONA).
  * Director                clamps that acknowledgement, runs the background evaluator on the transcript, applies the verdict through
                            the Interview state machine, and speaks the next question itself (TTSSpeakFrame, verbatim from the
                            role config). The acknowledgement plays while the evaluator works, so the evaluator is mostly hidden.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Callable

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from websockets.protocol import State

from evaluator import Evaluator
from guardrails import ACK_MAX_WORDS, AckVariety, clamp_ack
from interview import Action, ExtractJob, Interview
from persona import APOLOGY, VOICE_DOWN_MESSAGE
from transcript_filter import PromptEchoFilter
from turn_hygiene import is_incomplete

FATAL_API = re.compile(r"no credits|insufficient_quota|billing|invalid_api_key|incorrect api key|401|403", re.I)
API_ERROR = re.compile(r"no credits|insufficient_quota|quota|billing|rate.?limit|429|overloaded|server_error|50[0-4]", re.I)
SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


class MicTap(FrameProcessor):
    """Records every incoming mic frame: wall-clock arrival + PCM."""

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


class GreetingGate:
    """The greeting and disclosure cannot be skipped (CLAUDE.md section 7). Until the LAST sentence of the greeting has finished playing
    (and for MIN_OPEN_S after the mic first connected, to swallow connect clicks and pops) the mic is replaced by silence on its way to
    the realtime service: no VAD start, so no barge-in, no transcript, no acknowledgement, and no consent. The mic recorder sits before
    this gate, so the session wav still has the real audio."""

    MIN_OPEN_S = 1.5  # ignore the mic this long after it first connects
    TAIL_S = 0.3  # margin after the greeting's audio has finished playing
    FAILSAFE_S = 60.0  # never deaf for longer than this, whatever happens to the TTS signals

    def __init__(self, on_open: Callable[[], None] | None = None, clock: Callable[[], float] = time.time):
        self.on_open, self.clock = on_open, clock
        self.opened = False
        self.t_mic: float | None = None  # first mic frame
        self.audio_s = 0.0  # greeting audio received so far (seconds)
        self.audio_t0: float | None = None  # when the first greeting chunk arrived (it plays in real time from then)
        self.release_at: float | None = None  # greeting fully played at this time (set once the TTS reports it is done)

    def tts_audio(self, nbytes: int, sample_rate: int = 24000) -> None:
        if self.opened or self.release_at is not None:
            return
        now = self.clock()
        self.audio_t0 = self.audio_t0 or now
        self.audio_s += nbytes / 2 / sample_rate

    def tts_stopped(self) -> None:
        """TTS finished synthesising the greeting; it is heard until audio_t0 + audio_s (or now, if playback has caught up)."""
        if self.opened or self.release_at is not None:
            return
        now = self.clock()
        self.release_at = max(now, (self.audio_t0 or now) + self.audio_s) + self.TAIL_S

    def _ready(self, now: float) -> bool:
        if self.t_mic is not None and now - self.t_mic >= self.FAILSAFE_S:
            return True
        return bool(self.t_mic is not None and now - self.t_mic >= self.MIN_OPEN_S and self.release_at is not None and now >= self.release_at)

    def mic(self, audio: bytes) -> bytes:
        """What the realtime service gets for this mic frame: the audio once the gate is open, otherwise the same length of silence."""
        now = self.clock()
        if self.t_mic is None:
            self.t_mic = now
        if not self.opened and self._ready(now):
            self.opened = True
            if self.on_open:
                self.on_open()
        return audio if self.opened else bytes(len(audio))


class MicGate(FrameProcessor):
    def __init__(self, gate: GreetingGate):
        super().__init__()
        self.gate = gate

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM and not self.gate.opened:
            frame.audio = self.gate.mic(frame.audio)  # silence of the same length until the greeting has been heard
        await self.push_frame(frame, direction)


class GreetingTap(FrameProcessor):
    """After the speech output: tells the gate how much greeting audio there is and when the TTS has finished it."""

    def __init__(self, gate: GreetingGate):
        super().__init__()
        self.gate = gate

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSAudioRawFrame):
                self.gate.tts_audio(len(frame.audio), frame.sample_rate)
            elif isinstance(frame, TTSStoppedFrame):
                self.gate.tts_stopped()
        await self.push_frame(frame, direction)


class BotTap(FrameProcessor):
    """Records the BOT audio as it will be heard: each chunk is placed at max(arrival, end of the previous chunk) (the transport plays
    in real time), and an interruption cuts whatever had not been played yet. render_bot_audio() lays it on the mic's timeline."""

    def __init__(self, rec: dict):
        super().__init__()
        self.rec = rec
        rec.setdefault("segs", [])
        rec.setdefault("cursor", 0.0)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSAudioRawFrame):
                start = max(time.time(), self.rec["cursor"])
                self.rec["segs"].append([start, bytes(frame.audio), frame.sample_rate])
                self.rec["cursor"] = start + len(frame.audio) / 2 / frame.sample_rate
            elif isinstance(frame, InterruptionFrame):
                self._cut(time.time())
        await self.push_frame(frame, direction)

    def _cut(self, t: float) -> None:
        segs = self.rec["segs"]
        while segs and segs[-1][0] >= t:
            segs.pop()
        if segs:
            start, pcm, rate = segs[-1]
            keep = int(max(0.0, t - start) * rate) * 2
            if keep < len(pcm):
                segs[-1][1] = pcm[:keep]
        self.rec["cursor"] = min(self.rec["cursor"], t)


def render_bot_audio(rec: dict, t0: float, seconds: float, rate: int = 24000) -> bytes:
    """Bot audio (s16 mono) on the session timeline that starts at wall time t0."""
    n = int(max(seconds, rec.get("cursor", t0) - t0) * rate) + rate
    buf = bytearray(n * 2)
    for start, pcm, r in rec.get("segs", []):
        if r != rate or start < t0:
            continue
        off = int((start - t0) * rate) * 2
        buf[off:off + len(pcm)] = pcm[: max(0, len(buf) - off)]
    return bytes(buf)


def mix_pcm(a: bytes, b: bytes) -> bytes:
    import numpy as np
    n = max(len(a), len(b)) // 2
    x = np.zeros(n, dtype=np.int32)
    x[: len(a) // 2] += np.frombuffer(a, dtype=np.int16)
    x[: len(b) // 2] += np.frombuffer(b, dtype=np.int16)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


WATCH = (UserStartedSpeakingFrame, UserStoppedSpeakingFrame, TranscriptionFrame, LLMTextFrame, BotStartedSpeakingFrame,
         BotStoppedSpeakingFrame)


class EventTap(FrameProcessor):
    def __init__(self, name: str, log: list, on_event: Callable[[str], None] | None = None):
        super().__init__()
        self.tap_name, self.log, self.on_event, self.last = name, log, on_event, {}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        from pipecat.frames.frames import TTSAudioRawFrame
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, WATCH + (TTSAudioRawFrame,)):
            k, now = type(frame).__name__, time.time()
            if not isinstance(frame, (LLMTextFrame, TTSAudioRawFrame)) or now - self.last.get(k, 0) > 1.2:
                extra = frame.text if isinstance(frame, (TranscriptionFrame, LLMTextFrame)) else None
                self.log.append({"tap": self.tap_name, "kind": k, "t": now, "text": extra})
                if self.on_event:
                    self.on_event(k)
            self.last[k] = now
        await self.push_frame(frame, direction)


class EndTrace(FrameProcessor):
    """Debug only (TRACE_END=1): logs when an EndFrame passes this point of the pipeline."""

    def __init__(self, name: str):
        super().__init__()
        self.label = name

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, EndFrame):
            logger.info(f"EndFrame passing {self.label} ({direction.name})")
        await self.push_frame(frame, direction)


class InstrumentedRealtime(OpenAIRealtimeLLMService):
    """Logs OpenAI's own VAD events and can insert the controller's spoken lines into the server-side conversation."""

    vad_log: list | None = None
    warm_ws = None  # a websocket opened before the candidate connected (warm_pool.py); adopted in _connect

    async def _connect(self):
        ws, self.warm_ws = self.warm_ws, None
        if ws is not None and not self._websocket and ws.state is State.OPEN:
            self._websocket = ws
            self._receive_task = self.create_task(self._receive_task_handler())  # reads the queued session.created -> session.update
            return
        await super()._connect()

    _turn_ms: dict | None = None  # item_id -> [audio_start_ms, audio_end_ms] of each candidate utterance (server VAD)

    def audio_seconds(self, item_id: str | None) -> float | None:
        """Length of the audio behind a finished transcript, or None if unknown (the echo filter then falls back to text-only rules)."""
        rng = (self._turn_ms or {}).get(item_id or "")
        return (rng[1] - rng[0]) / 1000.0 if rng and rng[0] is not None and rng[1] is not None else None

    async def _handle_evt_speech_started(self, evt):
        if self._turn_ms is None:
            self._turn_ms = {}
        self._turn_ms.setdefault(getattr(evt, "item_id", ""), [None, None])[0] = evt.audio_start_ms
        if self.vad_log is not None:
            self.vad_log.append({"tap": "openai_vad", "kind": "speech_started", "t": time.time(), "audio_ms": evt.audio_start_ms})
        await super()._handle_evt_speech_started(evt)

    async def _handle_evt_speech_stopped(self, evt):
        if self._turn_ms is None:
            self._turn_ms = {}
        self._turn_ms.setdefault(getattr(evt, "item_id", ""), [None, None])[1] = evt.audio_end_ms
        if self.vad_log is not None:
            self.vad_log.append({"tap": "openai_vad", "kind": "speech_stopped", "t": time.time(), "audio_ms": evt.audio_end_ms})
        await super()._handle_evt_speech_stopped(evt)

    async def note_bot_line(self, text: str) -> None:
        """Tell the model what the controller just said (so its acknowledgements have the conversation). Marked 'manual' so
        Pipecat does not mistake it for a model response."""
        for _ in range(100):
            if self._api_session_ready:
                break
            await asyncio.sleep(0.1)
        item = events.ConversationItem(type="message", role="assistant", content=[events.ItemContent(type="output_text", text=text)])
        self._messages_added_manually[item.id] = True
        await self.send_client_event(events.ConversationItemCreateEvent(item=item))


class EchoGate(FrameProcessor):
    """Between the user aggregator and the realtime service. The service pushes the finished user transcript UPSTREAM; strip prompt
    echo there, swallow turns that were only echo/noise, and hand real turns to the director."""

    def __init__(self, log: list, echo_filter: PromptEchoFilter, on_turn: Callable[[str], None],
                 audio_of: Callable[[str | None], float | None] | None = None):
        super().__init__()
        self.log, self.filter, self.on_turn, self.audio_of = log, echo_filter, on_turn, audio_of

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.UPSTREAM and isinstance(frame, TranscriptionFrame):
            audio_s = self.audio_of(getattr(getattr(frame, "result", None), "item_id", None)) if self.audio_of else None
            res = self.filter.clean(frame.text, audio_s=audio_s)
            self.log.append({"tap": "user_text", "kind": "dropped" if res.dropped else "kept", "t": time.time(),
                             "raw": frame.text, "clean": res.text, "echo": res.echo_segments, "audio_s": audio_s})
            logger.info(f"USER TRANSCRIPT {'DROPPED' if res.dropped else 'kept'} ({'?' if audio_s is None else f'{audio_s:.1f}'}s audio): "
                        f"raw={frame.text!r} clean={res.text!r}")
            if res.dropped:
                return
            frame.text = res.text
            self.on_turn(res.text)
        await self.push_frame(frame, direction)


class Director(FrameProcessor):
    END_GRACE_S = 3.0  # how long the pipeline gets to end on its own before the session is finalised explicitly
    GRACE_S = 2.5  # a verdict held back because the candidate started talking again is spoken after this, unless a new turn supersedes it
    MIN_APPLY_S = 2.0  # no verdict is acted on sooner than this after the candidate stopped: a ~1.5 s thinking pause stays ONE answer
    LATE_MERGE_S = 3.0  # an INCOMPLETE turn ("All right, yeah so") is acted on no sooner than this after it ended ...
    CONTINUE_S = 1.2  # ... and no sooner than this after its verdict is ready; anything the candidate adds meanwhile is merged
    QUIET_BEFORE_REASK_S = 1.5  # "didn't catch that" is never spoken within this long of the candidate's last speech

    def __init__(self, interview: Interview, evaluator: Evaluator, brain: InstrumentedRealtime, log: list, worker_ref: dict,
                 silence_scale: float = 1.0, ack_timeout: float = 3.5):
        super().__init__()
        self.iv, self.ev, self.brain, self.log, self.worker_ref = interview, evaluator, brain, log, worker_ref
        self.silence_scale, self.ack_timeout = silence_scale, ack_timeout
        self.mute = 1  # the first model response (session kickoff) is never spoken: the greeting is scripted
        self.ack_buf, self.ack_done = "", False
        self.ack_variety = AckVariety()
        self.greeting_done = True  # start() sets False; the GreetingGate calls greeting_played() once the disclosure has been heard
        self.responses_ended = 0
        self.stop_marker = 0
        self.last_stop_wall = 0.0  # when the candidate's most recent utterance ended
        self.last_q_action_t = 0.0  # when the director last asked/re-asked/redirected (i.e. changed the pending question)
        self.user_talking = False
        self.bot_speaking = False
        self.turn_seq = 0
        self.held: tuple[int, object, int] | None = None
        self.finishing = False
        self.fatal = False
        self.voice_dead = False
        self._silence_task: asyncio.Task | None = None
        self._speaking_silence_line = False
        self._tasks: set[asyncio.Task] = set()
        self.bot_replies = 0
        self._clock_task: asyncio.Task | None = None
        self._ack_push_t = 0.0

    # ---- bookkeeping ---------------------------------------------------------------------------------------------------
    def _log(self, ev: str, **kw) -> None:
        self.log.append({"tap": "director", "kind": ev, "t": time.time(), **kw})

    async def _guarded(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a background task must never take the call down, nor fail silently
            logger.exception(f"director task failed: {e!r}")
            self._log("task_error", error=f"{type(e).__name__}: {str(e)[:200]}")

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(self._guarded(coro))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def shutdown(self) -> None:
        for t in list(self._tasks) + [self._silence_task, self._clock_task]:
            if t and not t.done():
                t.cancel()

    # ---- frames --------------------------------------------------------------------------------------------------------
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            if not self._speaking_silence_line:  # a check-in line must not cancel the ladder that is speaking it
                self._cancel_silence()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            self.bot_replies += 1
            self._on_bot_stopped()
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, UserStartedSpeakingFrame):
                self.user_talking = True
                if self.bot_speaking and time.time() - self._ack_push_t < 4.0:  # they resumed over our acknowledgement: it is cut by barge-in
                    self._log("ack_cut", after_s=round(time.time() - self._ack_push_t, 2))
                self._cancel_silence()
            elif isinstance(frame, UserStoppedSpeakingFrame):
                self.user_talking = False
                self.last_stop_wall = time.time()
                self.stop_marker = self.responses_ended
                self._log("user_stopped")
                if self.held and self.held[0] == self.turn_seq:
                    self._spawn(self._apply_held_later(self.held[0]))
            elif isinstance(frame, LLMTextFrame):
                await self._ack_text(frame)
                return
            elif isinstance(frame, LLMFullResponseEndFrame):
                await self._ack_end()
        await self.push_frame(frame, direction)

    # ---- acknowledgement clamp -----------------------------------------------------------------------------------------
    async def _ack_text(self, frame: LLMTextFrame) -> None:
        if self.mute > 0 or self.ack_done:
            return
        self.ack_buf += frame.text
        if SENTENCE_END.search(self.ack_buf) or len(self.ack_buf.split()) > ACK_MAX_WORDS + 2:
            await self._ack_flush()

    async def _ack_flush(self) -> None:
        raw, self.ack_buf, self.ack_done = self.ack_buf, "", True
        text, reason = clamp_ack(raw)
        if self.user_talking:  # they resumed while the model was still composing it: never speak an acknowledgement over the candidate
            self._log("ack_dropped", raw=raw[:200], why="candidate resumed")
            return
        text, reason = self.ack_variety.review(text, reason)  # thanks budget, no repeats, an occasional silent ack
        if not text:
            self._log("ack_skipped", raw=raw[:200], reason=reason)
            return
        if not text.rstrip().endswith((".", "!", "?")):
            text += "."
        self._log("ack", raw=raw[:200], text=text, reason=reason)
        self._ack_push_t = time.time()
        await self.push_frame(TTSSpeakFrame(text))  # every spoken line is one TTSSpeakFrame -> SpeechOutput (sentence-level failover)

    async def _ack_end(self) -> None:
        if self.mute > 0:
            self.mute -= 1
        elif not self.ack_done and self.ack_buf.strip():
            await self._ack_flush()
        self.ack_buf, self.ack_done = "", False
        self.responses_ended += 1

    async def _wait_ack(self, marker: int) -> bool:
        t0 = time.time()
        while self.responses_ended <= marker and time.time() - t0 < self.ack_timeout:
            await asyncio.sleep(0.03)
        ok = self.responses_ended > marker
        if not ok:
            self._log("ack_wait_timeout", waited=round(time.time() - t0, 2))
        return ok

    # ---- a candidate turn ----------------------------------------------------------------------------------------------
    def on_user_turn(self, text: str) -> None:
        """Called by EchoGate with a cleaned, non-empty transcript."""
        if self.finishing:
            return
        if not self.greeting_done:  # consent counts only after the full disclosure has played; nothing is repeated either
            self._log("turn_ignored_greeting", text=text[:80])
            return
        if self.last_stop_wall and self.last_stop_wall < self.last_q_action_t:
            # The utterance ended BEFORE the director asked the current question (a split turn: "Yeah." ... "sure, let's go"), so it
            # cannot be the answer to it. Keep it for the record; do not evaluate it or answer it.
            self._log("stale_fragment", text=text[:120], ended_before_question_s=round(self.last_q_action_t - self.last_stop_wall, 2))
            self.iv.note_late_fragment(text)
            return
        self.turn_seq += 1
        self.held = None
        self.iv.on_user_turn(text)
        inc = is_incomplete(text)
        self._log("turn", seq=self.turn_seq, incomplete=inc, words=len(text.split()), merged_with=len(self.iv.answer_turns) - 1)
        self._spawn(self._handle_turn(self.turn_seq, self.stop_marker, time.time(), inc, self.last_stop_wall))

    async def _handle_turn(self, seq: int, marker: int, t_arrive: float, inc: bool = False, stop_wall: float = 0.0) -> None:
        ctx = self.iv.eval_context()
        v = await self.ev.classify(ctx)
        self._log("verdict", seq=seq, step=ctx.step_id, step_kind=ctx.step_kind, post_probe=ctx.post_probe,
                  turn_kind=v.turn_kind, specificity=v.specificity, answers=v.answers_question, consent=v.consent,
                  knockout=v.knockout, cq=v.candidate_question, no_more=v.no_more_questions, failed=v.failed,
                  classify_s=round(v.latency_s, 2), since_transcript_s=round(time.time() - t_arrive, 2), usage=v.usage)
        if v.failed and FATAL_API.search(v.failed):
            await self.fatal_apology(v.failed)
            return
        if seq != self.turn_seq or self.finishing:
            self._log("verdict_superseded", seq=seq)
            return
        # Merge window: do not act on this turn until the candidate has had time to carry on. An incomplete turn waits longer.
        now = time.time()
        until = max(stop_wall + (self.LATE_MERGE_S if inc else self.MIN_APPLY_S), now + (self.CONTINUE_S if inc else 0.0))
        if until > now:
            self._log("continuation_wait", seq=seq, incomplete=inc, wait_s=round(until - now, 2))
            while time.time() < until and not self.user_talking:
                if seq != self.turn_seq or self.finishing:
                    self._log("verdict_superseded", seq=seq, during="continuation_wait")
                    return
                await asyncio.sleep(0.05)
            if seq != self.turn_seq or self.finishing:
                self._log("verdict_superseded", seq=seq, during="continuation_wait")
                return
        if inc and v.turn_kind == "unclear" and not self.user_talking:
            # A fragment is never re-asked: nothing new arrived, so stay quiet and let the silence ladder take over.
            self._log("fragment_no_reask", seq=seq)
            self._restart_silence()
            return
        if self.user_talking:  # they started again: hold the verdict; the next transcript supersedes it, else it is spoken after GRACE_S
            self.held = (seq, v, marker)
            self._log("verdict_held", seq=seq)
            return
        await self._apply(seq, v, marker)

    async def _apply_held_later(self, seq: int) -> None:
        await asyncio.sleep(self.GRACE_S)
        if self.held and self.held[0] == seq == self.turn_seq and not self.user_talking and not self.finishing:
            _, v, marker = self.held
            self.held = None
            self._log("verdict_held_released", seq=seq)
            await self._apply(seq, v, marker)

    async def _apply(self, seq: int, v, marker: int) -> None:
        await self._wait_ack(marker)
        if v.turn_kind == "unclear":  # "Sorry, I didn't catch that" is never said over, or right after, the candidate's speech
            while self.user_talking or time.time() - self.last_stop_wall < self.QUIET_BEFORE_REASK_S:
                if seq != self.turn_seq or self.finishing:
                    break
                await asyncio.sleep(0.05)
        if seq != self.turn_seq or self.user_talking or self.finishing:
            self._log("verdict_superseded", seq=seq, after_ack=True)
            return
        action = self.iv.decide(v)  # decide + speak without an await in between
        await self._do(action)

    # ---- speaking ------------------------------------------------------------------------------------------------------
    async def _do(self, a: Action) -> None:
        self._log("action", action_kind=a.kind, text=a.speak, step=self.iv.step.id, stage=self.iv.stage, end=a.end, reason=a.reason,
                  probe_used=sorted(self.iv.probe_used))
        if a.speak and a.kind in ("question", "probe", "reask", "redirect", "answer"):
            self.last_q_action_t = time.time()
        if a.speak:
            self._speaking_silence_line = a.kind == "silence"
            await self.push_frame(TTSSpeakFrame(a.speak))
            self._spawn(self.brain.note_bot_line(a.speak))
        elif a.kind == "defer" and not a.end:
            self._restart_silence()  # a deliberately silent action (repeated "can I ask a question?"): the ladder still covers dead air
        if a.extract:
            self._spawn(self._extract(a.extract))
        if a.end:
            self._spawn(self._finish())

    async def _extract(self, job: ExtractJob) -> None:
        x = await self.ev.extract(job.ctx)
        before = len(self.iv.evidence), len(self.iv.rejected_quotes)
        self.iv.apply_extraction(job, x)
        self._log("extraction", step=job.step.id, extract_s=round(x.latency_s, 2), failed=x.failed, proposed=len(x.evidence),
                  stored=len(self.iv.evidence) - before[0], rejected=len(self.iv.rejected_quotes) - before[1])

    def greeting_played(self) -> None:
        self.greeting_done = True
        self._log("greeting_played")

    async def start(self) -> None:
        text = self.iv.opening()
        self.greeting_done = False
        self._log("greeting")
        await self.push_frame(TTSSpeakFrame(text))
        self._spawn(self.brain.note_bot_line(text))
        self._clock_task = asyncio.create_task(self._guarded(self._clock()))

    async def _clock(self) -> None:
        while not self.finishing:
            await asyncio.sleep(2)
            a = self.iv.time_check()
            if a and not self.finishing:
                self._log("time_limit")
                await self._do(a)

    async def _finish(self, quick: bool = False) -> None:
        self.finishing = True
        self._cancel_silence()
        t0, seen = time.time(), quick
        while time.time() - t0 < (0 if quick else 50):
            if self.bot_speaking:
                seen = True
            elif seen or time.time() - t0 > 8:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.8)
        self._log("ending", reason=self.iv.ended)
        w = self.worker_ref.get("worker")
        if w:
            await w.queue_frames([EndFrame()])
        # Pipecat 1.12.0 (SmallWebRTC): the EndFrame reaches the INPUT transport first, which tears the peer connection down; the
        # OUTPUT transport then waits forever for its final silence write to be consumed, so the pipeline never finishes by itself
        # (found with EndTrace) and no on_client_disconnected fires. Finalise explicitly after a short grace period.
        await asyncio.sleep(self.END_GRACE_S)
        fin = self.worker_ref.get("finish")
        if fin:
            self._log("finalize")
            await fin()

    async def voice_failed(self, why: str) -> None:
        """Every TTS provider failed: nothing can be spoken. End cleanly and say so on screen (RTVI server message + a bot text line)."""
        if self.voice_dead:
            return
        self.voice_dead = self.finishing = True
        self.iv.flag("voice failure", why[:120])
        self.iv.ended = "error"
        self._log("voice_failed", why=why[:200])
        self._cancel_silence()
        await self.push_frame(RTVIServerMessageFrame(data={"type": "interview_ended", "reason": "voice_unavailable", "message": VOICE_DOWN_MESSAGE}))
        await self.push_frame(TTSTextFrame(text=VOICE_DOWN_MESSAGE, aggregated_by="sentence"))
        self._spawn(self._finish(quick=True))

    async def fatal_apology(self, why: str) -> None:
        if self.fatal:
            return
        self.fatal = True
        self.iv.flag("technical failure", why[:120])
        self.iv.ended = "error"
        self._log("fatal_apology", why=why[:200])
        self.finishing = False
        await self._do(Action(APOLOGY, "close", end=True, reason="error"))

    # ---- silence ladder (8 s / 20 s / 45 s after the bot stops) ---------------------------------------------------------
    def _cancel_silence(self) -> None:
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = None

    def _restart_silence(self) -> None:
        if self.finishing or self.iv.ended:
            return
        self._cancel_silence()
        self._silence_task = asyncio.create_task(self._guarded(self._ladder()))

    def _on_bot_stopped(self) -> None:
        if self.finishing or self.iv.ended:
            return
        if self._speaking_silence_line:  # a check-in must not restart the ladder
            self._speaking_silence_line = False
            return
        self._cancel_silence()
        self._silence_task = asyncio.create_task(self._guarded(self._ladder()))

    async def _ladder(self) -> None:
        t0 = time.time()
        for level, at in ((1, 8), (2, 20), (3, 45)):
            await asyncio.sleep(max(0, at * self.silence_scale - (time.time() - t0)))
            for _ in range(100):  # the previous check-in may still be playing; wait for it rather than abandoning the ladder
                if not self.bot_speaking:
                    break
                await asyncio.sleep(0.1)
            if self.finishing or self.user_talking or self.bot_speaking:
                return
            self._log("silence", level=level)
            await self._do(self.iv.silence_action(level))
            if level == 3:
                return
