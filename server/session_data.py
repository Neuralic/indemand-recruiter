"""Turn the engine's session file (<session>.json, written by the frozen engine's dump()) into what the product needs: a clean, timed
transcript on the recording's timeline, plus the engine's own interview record. Read-only: nothing here writes to the engine's files.

Timeline: the recording (<session>_mix.wav) starts at the first mic frame, and the engine logs OpenAI's `audio_ms` for every speech start
and stop, which IS the offset into that recording. Candidate turns are paired with those VAD segments in order. Bot lines are placed at
the first BotStartedSpeaking after they were queued.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from guardrails import redact_protected


@dataclass
class Turn:
    idx: int
    speaker: str  # "candidate" | "avery"
    text: str
    t_start: float  # seconds into the recording
    t_end: float


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _segments(events: list[dict]) -> list[tuple[float, float]]:
    segs, start = [], None
    for e in events:
        if e["tap"] != "openai_vad":
            continue
        if e["kind"] == "speech_started":
            start = e["audio_ms"] / 1000
        elif e["kind"] == "speech_stopped" and start is not None:
            segs.append((start, e["audio_ms"] / 1000))
            start = None
    return segs


def build_transcript(session: dict) -> list[Turn]:
    """Candidate turns (kept transcripts only) and Avery's spoken lines, in time order. Protected-ground words are redacted."""
    ev = session["events"]
    t0 = session["mic_frames"][0][0] if session.get("mic_frames") else ev[0]["t"]
    segs = _segments(ev)
    used: set[int] = set()
    rows: list[tuple[float, float, str, str]] = []

    for e in ev:
        if e["tap"] == "user_text":
            # pair with a VAD segment: the one whose length best matches the audio length, among those that ended before the transcript
            cand = [i for i, (s, p) in enumerate(segs) if i not in used and t0 + p <= e["t"] + 0.5]
            pick = None
            if e.get("audio_s") and cand:
                best = min(cand, key=lambda i: abs((segs[i][1] - segs[i][0]) - e["audio_s"]))
                if abs((segs[best][1] - segs[best][0]) - e["audio_s"]) < 0.4:
                    pick = best
            if pick is None and cand:
                pick = cand[0]
            if pick is not None:
                used.update(range(pick + 1))  # transcripts arrive in order: earlier segments with no transcript are noise
                s, p = segs[pick]
            else:
                s = p = max(0.0, e["t"] - t0)
            if e["kind"] == "kept" and (e.get("clean") or "").strip():
                rows.append((s, p, "candidate", e["clean"].strip()))
        elif e["tap"] == "director" and e["kind"] in ("ack", "action") and (e.get("text") or "").strip():
            started = next((b["t"] for b in ev if b["tap"] == "out" and b["kind"] == "BotStartedSpeakingFrame" and e["t"] <= b["t"] <= e["t"] + 4), e["t"])
            rows.append((started - t0, started - t0 + max(1.0, len(e["text"].split()) / 2.6), "avery", e["text"].strip()))

    greeting = next((t for who, t in session.get("transcript", []) if who == "bot"), "")
    g = next((b["t"] for b in ev if b["tap"] == "out" and b["kind"] == "BotStartedSpeakingFrame"), ev[0]["t"])
    if greeting:
        rows.append((g - t0, g - t0 + 28.0, "avery", greeting))
    rows.sort(key=lambda r: (r[0], 0 if r[2] == "avery" else 1))
    out = []
    for i, (s, p, who, text) in enumerate(rows):
        out.append(Turn(i, who, redact_protected(text)[0] if who == "candidate" else text, round(max(0.0, s), 2), round(max(s, p), 2)))
    return out


def recording_paths(json_path: Path) -> dict[str, Path]:
    stem = Path(json_path).with_suffix("")
    return {k: Path(f"{stem}_{k}.wav") for k in ("mix", "mic", "bot")}


def duration_s(session: dict) -> float:
    return float(session["interview"].get("elapsed_s") or 0)
