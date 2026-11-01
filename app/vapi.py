"""Server-side Vapi API client: fetch a finished call and turn it into transcript turns. Uses the PRIVATE key; never reaches the browser."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

import app.config as cfg

_ID_RE = re.compile(r"^[0-9a-fA-F-]{16,64}$")
CANDIDATE_ROLES = {"user"}
AVERY_ROLES = {"bot", "assistant"}


class VapiError(Exception):
    pass


class NotOurCall(VapiError):
    """The call exists but belongs to a different assistant."""


@dataclass
class CallData:
    call_id: str
    status: str
    ended_reason: str
    started_at: float | None
    ended_at: float | None
    duration_s: float | None
    turns: list[dict] = field(default_factory=list)  # [{speaker: candidate|interviewer, text, t_start, t_end}]
    has_recording: bool = False


def valid_call_id(s: str) -> bool:
    return bool(_ID_RE.match(s or ""))


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _headers() -> dict:
    if not cfg.VAPI_PRIVATE_KEY:
        raise VapiError("VAPI_PRIVATE_KEY is not configured")
    return {"Authorization": f"Bearer {cfg.VAPI_PRIVATE_KEY}"}


def turns_from_messages(messages: list[dict], call_seconds: float | None = None, started_at: float | None = None) -> list[dict]:
    """Vapi artifact.messages -> turns. Keeps only spoken lines (user / bot), merges back-to-back lines of one speaker.

    TIMING (verified against a real call's audio tracks, 55 spoken messages): `endTime` (epoch ms) and `duration` (ms) place a message in
    the recording: [endTime - duration, endTime] minus the call's startedAt overlapped real speech for 55/55 messages (median end error
    0.17 s; the start can run 1-3 s early because `duration` includes some lead-in). `secondsFromStart` and `time` are NOT speech times:
    they were 9 to 33 s late (median 14 s) and drifted, so they are used only if endTime/duration are missing.
    """
    spoken = []
    for m in messages or []:
        role = (m.get("role") or "").lower()
        text = " ".join(str(m.get("message") or m.get("content") or "").split())
        if not text or (role not in CANDIDATE_ROLES and role not in AVERY_ROLES):
            continue
        end, dur = m.get("endTime"), m.get("duration")
        if started_at and isinstance(end, (int, float)) and isinstance(dur, (int, float)) and dur > 0:
            t_end = max(0.0, end / 1000.0 - started_at)
            t_start = max(0.0, t_end - dur / 1000.0)
        else:  # fallback: late, but better than nothing
            t_start = float(m.get("secondsFromStart") or 0.0)
            t_end = t_start + (float(dur) / 1000.0 if isinstance(dur, (int, float)) else 0.0)
        spoken.append({"speaker": "candidate" if role in CANDIDATE_ROLES else "interviewer", "text": text,
                       "t_start": round(t_start, 2), "t_end": round(t_end, 2)})
    spoken.sort(key=lambda t: t["t_start"])
    merged: list[dict] = []
    for t in spoken:
        if merged and merged[-1]["speaker"] == t["speaker"]:
            merged[-1]["text"] += " " + t["text"]
            merged[-1]["t_end"] = max(merged[-1]["t_end"], t["t_end"])
        else:
            merged.append(dict(t))
    for i, t in enumerate(merged):  # fill missing end times with the next turn's start
        if t["t_end"] <= t["t_start"]:
            nxt = merged[i + 1]["t_start"] if i + 1 < len(merged) else (call_seconds or t["t_start"])
            t["t_end"] = max(t["t_start"], nxt)
    return merged


def parse_call(raw: dict) -> CallData:
    art = raw.get("artifact") or {}
    started, ended = _ts(raw.get("startedAt")), _ts(raw.get("endedAt"))
    dur = (ended - started) if started and ended else None
    turns = turns_from_messages(art.get("messages") or raw.get("messages") or [], dur, started)
    has_rec = bool(art.get("recording") or art.get("recordingUrl") or art.get("stereoRecordingUrl")
                   or art.get("presignedMonoUrl") or art.get("presignedStereoUrl"))
    return CallData(call_id=raw.get("id", ""), status=raw.get("status", ""), ended_reason=raw.get("endedReason") or "",
                    started_at=started, ended_at=ended, duration_s=dur, turns=turns, has_recording=has_rec)


async def fetch_raw(call_id: str, client: httpx.AsyncClient | None = None) -> dict:
    if not valid_call_id(call_id):
        raise VapiError("bad call id")
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.get(f"{cfg.VAPI_API_BASE}/call/{call_id}", headers=_headers())
    finally:
        if own:
            await client.aclose()
    if r.status_code == 404:
        raise VapiError("call not found")
    if r.status_code >= 400:
        raise VapiError(f"Vapi API {r.status_code}")
    return r.json()


def _check_ours(raw: dict) -> None:
    if cfg.VAPI_ASSISTANT_ID and raw.get("assistantId") != cfg.VAPI_ASSISTANT_ID:
        raise NotOurCall("call belongs to another assistant")


async def fetch_finished_call(call_id: str, wait_s: float = 60.0, poll_s: float = 2.5) -> CallData:
    """Fetch the call; wait (up to wait_s) until it has ended and its transcript has been written. Raises NotOurCall for foreign calls."""
    deadline = time.monotonic() + wait_s
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            raw = await fetch_raw(call_id, client)
            _check_ours(raw)
            last = parse_call(raw)
            if last.status == "ended" and last.turns:
                return last
            if time.monotonic() >= deadline:
                if last.status == "ended":
                    return last  # ended with no spoken lines: the caller shows "no usable transcript"
                raise VapiError("call has not finished")
            await asyncio.sleep(poll_s)


def pick_recording_url(artifact: dict) -> str | None:
    rec = artifact.get("recording") if isinstance(artifact.get("recording"), dict) else {}
    stereo = rec.get("stereoUrl") or (rec.get("stereo") or {}).get("url") if rec else None
    mono = (rec.get("mono") or {}).get("combinedUrl") if rec and isinstance(rec.get("mono"), dict) else None
    cand = [artifact.get("presignedStereoUrl"), artifact.get("presignedMonoUrl"), stereo, mono,
            artifact.get("stereoRecordingUrl"), artifact.get("recordingUrl")]
    return next((u for u in cand if isinstance(u, str) and u.startswith("http")), None)


async def recording_url(call_id: str) -> str | None:
    """A fresh audio URL for the call, fetched server side. Prefers the stereo file (both sides)."""
    raw = await fetch_raw(call_id)
    _check_ours(raw)
    return pick_recording_url(raw.get("artifact") or {})
