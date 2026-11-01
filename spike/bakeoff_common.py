"""Phase 0b bake-off: shared script, voices, and TTS service factories (spike only)."""

from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv

import harness as h

load_dotenv(override=True)
BAKE = h.OUT / "bakeoff"
BAKE.mkdir(exist_ok=True)

# 10 bot lines from CLAUDE.md §6. Together they say all nine required terms aloud.
LINES = [
    ("disclosure", "Hi Dana, I'm Avery, IN DEMAND's AI screening assistant. This is a short first conversation about "
                   "the Electrical Engineering Supervisor role. It's recorded so Stefanie's team can review it "
                   "personally, and a human reviews every interview. Shall we start?"),
    ("motivation", "Thanks, Dana. To start, what's got you open to a move right now, and what do you already know "
                   "about this role?"),
    ("background", "You mentioned you ran the reliability program at the gas plant. How did you roll out RCM across "
                   "the site, and what changed in how maintenance work got planned?"),
    ("probe", "That's helpful. Can you give me one specific example where you improved MTBF on a critical asset? "
              "What did you personally do, and what was the number before and after?"),
    ("competency_loto", "How do you run your LOTO program day to day, and what do you do when a crew pushes back on "
                        "the process?"),
    ("competency_systems", "Tell me about your experience with SCADA upgrades, and the work orders you managed in "
                           "Maximo or another CMMS."),
    ("logistics_licence", "Do you hold, or are you eligible for, P.Eng registration with APEGA? And do you have your "
                          "Red Seal?"),
    ("logistics_auth", "Are you legally eligible to work in Canada, and what notice period would you need to give "
                       "your current employer?"),
    ("candidate_q", "That's a great one for Stefanie, so I'll make sure she covers the bonus structure with you on "
                    "your call. What I can tell you is that the role is based in Edmonton."),
    ("close", "Thanks so much, Dana. Stefanie's team reviews every interview within one business day, and she'll be "
              "in touch about next steps. Take care."),
]
TERMS = ["APEGA", "P.Eng", "Maximo", "SCADA", "MTBF", "CMMS", "LOTO", "RCM", "Red Seal"]
DOMAIN_TERMS = ["APEGA", "P.Eng", "CEC", "arc flash", "MCC", "switchgear", "PLC", "SCADA", "LOTO", "CMMS", "SAP PM",
                "Maximo", "RCM", "MTBF", "Red Seal"]
TRANSCRIBE_PROMPT = "Industrial maintenance and electrical engineering interview. Terms: " + ", ".join(DOMAIN_TERMS) + "."

_all = " ".join(t for _, t in LINES)
for _t in TERMS:
    assert _t in _all, f"script is missing required term {_t}"

SHORTLIST = json.loads((h.OUT / "08_shortlist.json").read_text(encoding="utf-8"))


def voices(provider: str) -> dict[str, str]:
    """name -> provider voice id."""
    return {n: v["id"] for n, v in SHORTLIST[provider].items()}


def make_tts(provider: str, voice_id: str, **extra):
    """The real Pipecat TTS service for a provider (websocket), at 24 kHz."""
    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService
        settings = CartesiaTTSService.Settings(model="sonic-3.6", voice=voice_id, **extra)
        return CartesiaTTSService(api_key=os.environ["CARTESIA_API_KEY"], sample_rate=24000, settings=settings)
    if provider == "fish":
        from pipecat.services.fish.tts import FishAudioTTSService
        model = os.environ.get("FISH_MODEL_OVERRIDE", "s2.1-pro")
        settings = FishAudioTTSService.Settings(model=model, voice=voice_id, **extra)
        return FishAudioTTSService(api_key=os.environ["FISH_API_KEY"], sample_rate=24000, settings=settings)
    raise ValueError(provider)


def pct(xs: list[float], p: float) -> float | None:
    """Nearest-rank percentile (n is small; no interpolation)."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, int(-(-p * len(xs) // 100)) - 1))
    return round(xs[k], 3)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def free_ram_mb() -> int:
    import ctypes

    class MEM(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

    m = MEM()
    m.dwLength = ctypes.sizeof(MEM)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return int(m.ullAvailPhys / 1048576)


class LoopLag:
    """Event-loop stall watchdog: max gap beyond the 0.1s tick. A run with lag > 2s is not trustworthy (memory-driven stalls
    killed sockets in Phase 0)."""

    def __init__(self) -> None:
        import asyncio
        self.max_lag = 0.0
        self.events: list[tuple[float, float, float]] = []  # (wall start, wall end, lag_s) for every stall > 1 s
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        import asyncio
        import time
        while True:
            t = time.perf_counter()
            await asyncio.sleep(0.1)
            d = time.perf_counter() - t - 0.1
            self.max_lag = max(self.max_lag, d)
            if d > 1.0:
                self.events.append((time.time() - d, time.time(), d))

    def overlaps(self, t0: float, t1: float) -> bool:
        return any(a < t1 and b > t0 for a, b, _ in self.events)

    def stop(self) -> float:
        self._task.cancel()
        return round(self.max_lag, 3)
