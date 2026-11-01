"""Speech output with a provider fallback chain (CLAUDE.md §14, David 2026-10-25).

  Cartesia sonic-3.6 -> Fish s2.1-pro-free   (native OpenAI marin voice stays a later last resort)

Every spoken line reaches SpeechOutput as a TTSSpeakFrame (the director owns all speech). SpeechOutput splits it into sentences and
speaks them ONE AT A TIME, so the switch can only ever happen BETWEEN sentences:

  * a provider fails BEFORE the sentence's first audio byte (HTTP 402/quota, connection error, or no first byte within the stall
    limit): nothing was audible, so the same sentence is simply spoken again by the next provider;
  * a provider fails AFTER audio has started: that sentence stays truncated (it is never repeated or restarted) and the NEXT
    sentence is spoken by the next provider;
  * once switched, the session stays on the fallback (no flapping back). Each switch is logged.
  * a failure that is not out-of-credits (stall, dropped socket, timeout, error) is first RETRIED ONCE on a fresh connection of the
    same provider, so one network blip never causes a switch (a quota error switches at once); Fish keeps a warm connection so the
    fallback does not pay a cold connect;
  * if the last provider also fails (after its own retry) the call is ended cleanly via `on_failure` (the director shows an
    on-screen message).

The audio providers are plain async clients (no Pipecat service objects), which is what makes sentence-level control possible and
lets automated tests run on a stub provider that spends no credits.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import struct
import time
import uuid
from typing import AsyncIterator, Awaitable, Callable

from loguru import logger

SAMPLE_RATE = 24000
STALL_FIRST_BYTE_S = float(os.environ.get("TTS_STALL_S", "3.0"))  # David: switch on a first-byte stall over 3 s
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]


class ProviderError(Exception):
    """kind: quota (HTTP 402/429/401/403 or an out-of-credits message) | disconnect | stall | error."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


class Provider:
    name = "provider"
    first_byte_timeout = STALL_FIRST_BYTE_S
    chunk_timeout = 3.0
    retry_kinds = ("stall", "disconnect", "error")  # failures worth one retry on a fresh connection (quota is never retried)

    def start(self) -> None:
        """Begin any background keep-warm work (called when the first sentence is spoken)."""

    async def reset(self) -> None:
        """Drop the current connection so the next synth() opens a fresh one."""

    def synth(self, text: str) -> AsyncIterator[bytes]:  # async generator of 24 kHz s16 mono PCM
        raise NotImplementedError

    async def prepare(self) -> None:
        """Open/refresh the connection BEFORE the first-byte clock starts (a cold connect is ~3 s and is not a synthesis stall)."""

    async def cancel(self) -> None:
        """Stop an in-flight sentence (barge-in or abandoned after a failure)."""

    async def aclose(self) -> None:
        pass


# ----------------------------------------------------------------------------------------------------------------------------
class CartesiaProvider(Provider):
    name = "cartesia"

    def __init__(self, tts_service, warm_ws=None):
        """`tts_service` is a configured (never started) pipecat CartesiaTTSService, used only to build the exact request the
        Phase 0/1 tests used (model, voice, output format); no Pipecat runtime is involved."""
        tts_service._output_sample_rate = SAMPLE_RATE
        self._template = json.loads(tts_service._build_msg(text="x", context_id="x"))
        self._url = tts_service._url
        self._headers = {"X-API-Key": tts_service._api_key, "Cartesia-Version": tts_service._cartesia_version,
                         **tts_service._extra_headers}
        self.ws = warm_ws
        self._cid: str | None = None

    async def _ensure(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        from websockets.protocol import State
        if self.ws is not None and self.ws.state is State.OPEN:
            return
        try:
            self.ws = await asyncio.wait_for(connect(self._url, additional_headers=self._headers), 5)
        except InvalidStatus as e:
            code = e.response.status_code
            raise ProviderError("quota" if code in (401, 402, 403, 429) else "error", f"HTTP {code} on connect") from e
        except Exception as e:  # noqa: BLE001
            raise ProviderError("disconnect", f"connect failed: {type(e).__name__}") from e

    async def prepare(self) -> None:
        await self._ensure()

    async def synth(self, text: str) -> AsyncIterator[bytes]:
        from websockets.exceptions import ConnectionClosed
        await self._ensure()
        cid = self._cid = uuid.uuid4().hex
        msg = dict(self._template, transcript=text, context_id=cid)
        msg["continue"] = False
        try:
            await self.ws.send(json.dumps(msg))
            while True:
                m = json.loads(await self.ws.recv())
                if m.get("context_id") != cid:
                    continue  # late output from an abandoned sentence
                t = m.get("type")
                if t == "chunk":
                    yield base64.b64decode(m["data"])
                elif t == "error":
                    sc, text_ = m.get("status_code"), str(m.get("message", ""))
                    quota = sc in (402, 429) or "credit" in text_.lower() or "quota" in text_.lower()
                    raise ProviderError("quota" if quota else "error", f"HTTP {sc} {text_[:80]}")
                if m.get("done"):
                    return
        except ConnectionClosed as e:
            raise ProviderError("disconnect", f"websocket closed ({e.rcvd.code if e.rcvd else 'no close frame'})") from e

    async def reset(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            asyncio.create_task(self._close_quietly(ws))  # a close handshake on a dead network can take 10 s; do not wait for it

    @staticmethod
    async def _close_quietly(ws) -> None:
        try:
            await asyncio.wait_for(ws.close(), 2)
        except Exception:  # noqa: BLE001
            pass

    async def cancel(self) -> None:
        try:
            if self.ws is not None and self._cid:
                await self.ws.send(json.dumps({"context_id": self._cid, "cancel": True}))
        except Exception:  # noqa: BLE001
            pass

    async def aclose(self) -> None:
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class FishProvider(Provider):
    name = "fish"
    URL = "https://api.fish.audio/v1/tts"

    KEEPALIVE_EVERY_S = 15.0

    def __init__(self, api_key: str, voice_id: str, model: str = "s2.1-pro-free"):
        self._client = self._new_client()
        self._headers = {"Authorization": f"Bearer {api_key}", "model": model}
        self._body = {"reference_id": voice_id, "format": "pcm", "sample_rate": SAMPLE_RATE, "latency": "balanced", "normalize": True}
        self.first_byte_timeout = 8.0  # Fish is the last resort: give it longer before giving up
        self.retry_kinds = ("disconnect", "error")  # a first-byte stall is not retried: that would double an 8 s wait
        self._keepalive: asyncio.Task | None = None
        self.warm = False

    @staticmethod
    def _new_client():
        import httpx
        return httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0),
                                 limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=90.0))

    def start(self) -> None:
        """Keep a TLS connection to Fish open for the whole session, so switching to it costs no handshake."""
        if self._keepalive is None:
            self._keepalive = asyncio.create_task(self._keep_warm())

    async def _keep_warm(self) -> None:
        while True:
            try:
                await self._client.head(self.URL)  # any response (even 405) leaves the connection pooled and warm
                self.warm = True
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next synth() reports a real failure; this loop must never raise
                self.warm = False
            await asyncio.sleep(self.KEEPALIVE_EVERY_S)

    async def reset(self) -> None:
        old, self._client = self._client, self._new_client()
        asyncio.create_task(self._aclose_client(old))
        self.warm = False

    @staticmethod
    async def _aclose_client(c) -> None:
        try:
            await asyncio.wait_for(c.aclose(), 2)
        except Exception:  # noqa: BLE001
            pass

    async def synth(self, text: str) -> AsyncIterator[bytes]:
        import httpx
        carry = b""
        try:
            async with self._client.stream("POST", self.URL, json=dict(self._body, text=text), headers=self._headers) as r:
                if r.status_code != 200:
                    quota = r.status_code in (401, 402, 403, 429)
                    raise ProviderError("quota" if quota else "error", f"HTTP {r.status_code}")
                async for ch in r.aiter_bytes():
                    ch = carry + ch
                    carry = ch[len(ch) // 2 * 2:]  # keep sample alignment
                    if len(ch) >= 2:
                        yield ch[: len(ch) // 2 * 2]
        except httpx.HTTPError as e:
            raise ProviderError("disconnect", f"{type(e).__name__}") from e

    async def aclose(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
        await self._client.aclose()


# ----------------------------------------------------------------------------------------------------------------------------
class StubProvider(Provider):
    """Spends no credits. Realistic timing: a first-byte delay like the real provider, then a faint 220 Hz hum for len(text)/14.5 s
    (the measured speech rate), generated about 6x faster than real time like a streaming TTS. It is a hum, not silence, ONLY so the
    headless test client can detect "the bot is speaking"; no human is on the line in stub mode.

    Fault injection (tests): faults="402@12,stall@20,disconnect@30,error@40" - kind@N, N = this provider's 0-based sentence count.
    """

    CPS = 14.5

    def __init__(self, name: str, ttfb: float, faults: str = "", stall_s: float = 3.6):
        self.name, self.ttfb, self.stall_s, self.n = name, ttfb, stall_s, 0
        self.faults = {int(n): k for k, n in (f.split("@") for f in faults.split(",") if f.strip())}
        self.spoken: list[str] = []
        self.resets = 0

    async def reset(self) -> None:
        self.resets += 1

    async def synth(self, text: str) -> AsyncIterator[bytes]:
        idx, self.n = self.n, self.n + 1
        fault = self.faults.get(idx)
        if fault == "402":
            raise ProviderError("quota", "HTTP 402 (injected)")
        if fault == "error":
            raise ProviderError("error", "injected")
        await asyncio.sleep(self.stall_s if fault == "stall" else self.ttfb)
        self.spoken.append(text)
        total = int(len(text) / self.CPS * SAMPLE_RATE)
        step = int(0.2 * SAMPLE_RATE)
        done = 0
        while done < total:
            n = min(step, total - done)
            chunk = struct.pack(f"<{n}h", *[int(2500 * math.sin(2 * math.pi * 220 * (done + i) / SAMPLE_RATE)) for i in range(n)])
            done += n
            yield chunk
            if fault == "disconnect" and done >= total // 2:
                raise ProviderError("disconnect", "injected mid-sentence")
            await asyncio.sleep(0.2 / 6)


# ----------------------------------------------------------------------------------------------------------------------------
class FailoverCore:
    """The switching logic, with no Pipecat dependency (unit-tested directly)."""

    def __init__(self, providers: list[Provider], log: list | None = None):
        self.providers, self.idx, self.log = providers, 0, log if log is not None else []
        self.failed = False
        self.sentences = 0

    @property
    def active(self) -> Provider:
        return self.providers[self.idx]

    def _event(self, kind: str, **kw) -> None:
        self.log.append({"tap": "tts_failover", "kind": kind, "t": time.time(), **kw})
        logger.warning(f"TTS failover: {kind} {kw}")

    def start(self) -> None:
        for p in self.providers:
            p.start()

    async def speak_sentence(self, sentence: str, emit: Callable[[bytes], Awaitable[None]]) -> bool:
        """Speak one sentence. `emit(chunk)` receives audio. Returns False only if every provider has failed."""
        retried: set[int] = set()  # providers already retried for THIS sentence
        while True:
            prov = self.active
            started = False
            t_send = time.time()
            logger.debug(f"Generating TTS [{sentence}] via {prov.name}")  # also what the test client watches for
            agen = prov.synth(sentence)
            try:
                try:
                    try:
                        await asyncio.wait_for(prov.prepare(), 6)
                    except asyncio.TimeoutError:
                        raise ProviderError("disconnect", "connect timed out after 6s") from None
                    while True:
                        timeout = prov.chunk_timeout if started else prov.first_byte_timeout
                        try:
                            chunk = await asyncio.wait_for(agen.__anext__(), timeout)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise ProviderError("stall", f"no {'audio' if started else 'first byte'} for {timeout:.1f}s") from None
                        started = True
                        await emit(chunk)
                    self.sentences += 1
                    return True
                except ProviderError:
                    raise
                except asyncio.CancelledError:
                    await prov.cancel()
                    raise
                except Exception as e:  # noqa: BLE001 - anything unexpected from a provider counts as a provider error
                    raise ProviderError("error", f"{type(e).__name__}: {str(e)[:80]}") from e
            except ProviderError as e:
                await prov.cancel()
                try:
                    await agen.aclose()
                except Exception:  # noqa: BLE001
                    pass
                if not started and e.kind in prov.retry_kinds and self.idx not in retried:
                    retried.add(self.idx)  # one transient failure must not switch voices or end the call: retry on a fresh connection
                    self._event("retry", provider=prov.name, reason=e.kind, detail=e.detail[:120], sentence=sentence[:60])
                    await prov.reset()
                    continue
                nxt = self.idx + 1
                if nxt >= len(self.providers):
                    self.failed = True
                    self._event("all_providers_failed", provider=prov.name, reason=e.kind, detail=e.detail[:120], sentence=sentence[:60])
                    return False
                self._event("switch", **{"from": prov.name, "to": self.providers[nxt].name}, reason=e.kind, detail=e.detail[:120],
                            mid_sentence=started, sentence=sentence[:60],
                            action="truncated sentence left as is; next sentence on fallback" if started
                            else "same sentence re-spoken on fallback")
                self.idx = nxt  # sticky: never switches back this session
                if started:
                    self.sentences += 1
                    return True
                # nothing was audible: loop and speak the same sentence with the fallback


# ----------------------------------------------------------------------------------------------------------------------------
def make_speech_output(core: FailoverCore, on_failure: Callable[[str], Awaitable[None]]):
    """Build the Pipecat processor lazily so importing this module (and unit-testing the core) does not need Pipecat running."""
    from pipecat.frames.frames import (CancelFrame, EndFrame, Frame, InterruptionFrame, LLMTextFrame, TTSAudioRawFrame,
                                       TTSSpeakFrame, TTSStartedFrame, TTSStoppedFrame, TTSTextFrame)
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    class SpeechOutput(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.core, self.on_failure = core, on_failure
            self._q: asyncio.Queue[str] = asyncio.Queue()
            self._worker: asyncio.Task | None = None
            self._idle = asyncio.Event()
            self._idle.set()
            self._cid: str | None = None
            self._started = False
            self._sentence_audio = False

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, TTSSpeakFrame) and direction == FrameDirection.DOWNSTREAM:
                if frame.text.strip() and not self.core.failed:
                    self.core.start()
                    self._idle.clear()
                    self._q.put_nowait(frame.text)
                    if self._worker is None or self._worker.done():
                        self._worker = asyncio.create_task(self._run())
                return
            if isinstance(frame, LLMTextFrame):
                return  # all speech arrives as TTSSpeakFrame; raw model text is never voiced
            if isinstance(frame, InterruptionFrame):
                await self._interrupt()
            elif isinstance(frame, EndFrame):
                try:
                    await asyncio.wait_for(self._idle.wait(), 20)
                except asyncio.TimeoutError:
                    pass
                await self._shutdown()
            elif isinstance(frame, CancelFrame):
                await self._interrupt()
                await self._shutdown()
            await self.push_frame(frame, direction)

        async def _emit(self, chunk: bytes):
            self._sentence_audio = True
            if not self._started:
                self._started = True
                await self.push_frame(TTSStartedFrame(context_id=self._cid))
            await self.push_frame(TTSAudioRawFrame(audio=chunk, sample_rate=SAMPLE_RATE, num_channels=1, context_id=self._cid))

        async def _run(self):
            try:
                while True:
                    text = self._q.get_nowait() if not self._q.empty() else None
                    if text is None:
                        break
                    self._cid, self._started = uuid.uuid4().hex, False
                    for s in split_sentences(text):
                        if self.core.failed:
                            break
                        self._sentence_audio = False
                        ok = await self.core.speak_sentence(s, self._emit)
                        if self._sentence_audio:  # text for the UI transcript, only for sentences that were actually voiced
                            await self.push_frame(TTSTextFrame(text=s, aggregated_by="sentence", context_id=self._cid))
                        if not ok:
                            break
                    if self._started:
                        await self.push_frame(TTSStoppedFrame(context_id=self._cid))
                    if self.core.failed:
                        while not self._q.empty():
                            self._q.get_nowait()
                        await self.on_failure("all TTS providers failed")
                        break
            except asyncio.CancelledError:
                raise
            finally:
                self._idle.set()

        async def _interrupt(self):
            while not self._q.empty():
                self._q.get_nowait()
            if self._worker and not self._worker.done():
                self._worker.cancel()
                try:
                    await self._worker
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                if self._started:
                    await self.push_frame(TTSStoppedFrame(context_id=self._cid))
                    self._started = False
            self._worker = None
            self._idle.set()

        async def _shutdown(self):
            for p in self.core.providers:
                await p.aclose()

    return SpeechOutput()
