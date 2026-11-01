"""Pre-warm (CLAUDE.md §14): keep one OpenAI Realtime websocket and one Cartesia websocket open BEFORE the candidate clicks Connect, so
the pipeline does not pay the two TLS+WebSocket handshakes (~2-3 s) on the critical path to the greeting.

Pipecat 1.12.0 opens both connections inside the service lifecycle, but both skip connecting when `_websocket` is already set
(Cartesia: `_connect_websocket` returns if it is OPEN; OpenAI: `_connect` returns early) - see pipeline_parts.InstrumentedRealtime
for the adopt step. A warm set is single-use and expires after MAX_AGE_S (Cartesia closes idle sockets after 5 minutes). If no warm
set is available the session simply connects cold, exactly as before.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from loguru import logger
from websockets.asyncio.client import connect as ws_connect
from websockets.protocol import State

MAX_AGE_S = 200.0  # a set older than this is never handed out (Cartesia closes idle sockets after 5 minutes)
REFRESH_AT_S = 90.0  # build the replacement while the current set is still good, so there is never a gap
CHECK_EVERY_S = 3.0


@dataclass
class WarmSet:
    openai_ws: object
    cartesia_ws: object
    born: float = field(default_factory=time.time)

    def usable(self) -> bool:
        return (time.time() - self.born < MAX_AGE_S and self.openai_ws.state is State.OPEN
                and self.cartesia_ws.state is State.OPEN)

    async def close_cartesia(self) -> None:
        try:
            await self.cartesia_ws.close()
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        for ws in (self.openai_ws, self.cartesia_ws):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass


class WarmPool:
    """Holds at most one warm set. `take()` hands it out (or None) and the refill loop builds the next one."""

    def __init__(self, openai_url: str, openai_headers: dict, cartesia_url: str, cartesia_headers: dict):
        self.openai_url, self.openai_headers = openai_url, openai_headers
        self.cartesia_url, self.cartesia_headers = cartesia_url, cartesia_headers
        self._set: WarmSet | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self.stats = {"built": 0, "taken": 0, "cold": 0, "expired": 0, "build_failed": 0}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
        if self._set:
            await self._set.close()

    def take(self) -> WarmSet | None:
        s, self._set = self._set, None
        if s is not None and s.usable():
            self.stats["taken"] += 1
            return s
        if s is not None:
            self.stats["expired"] += 1
            asyncio.create_task(s.close())
        self.stats["cold"] += 1
        return None

    async def _build(self) -> WarmSet:
        t0 = time.time()
        o, c = await asyncio.gather(ws_connect(self.openai_url, additional_headers=self.openai_headers),
                                    ws_connect(self.cartesia_url, additional_headers=self.cartesia_headers))
        logger.info(f"warm pool: built a warm set in {time.time() - t0:.2f}s")
        return WarmSet(o, c)

    async def _loop(self) -> None:
        while not self._stop:
            try:
                cur = self._set
                stale = cur is None or time.time() - cur.born > REFRESH_AT_S or not cur.usable()
                if stale:
                    new = await self._build()  # the old set (if any) stays available to take() while this builds
                    old, self._set = self._set, new
                    self.stats["built"] += 1
                    if old is not None:
                        asyncio.create_task(old.close())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - never take the server down; sessions fall back to cold connects
                self.stats["build_failed"] += 1
                logger.warning(f"warm pool: build failed ({type(e).__name__}: {str(e)[:80]}); sessions will connect cold")
                await asyncio.sleep(10)
            await asyncio.sleep(CHECK_EVERY_S)
