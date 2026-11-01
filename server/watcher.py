"""Picks up finished sessions from the (frozen) engine's output folder, stores them and starts the scorecard. Poll-based on purpose:
the engine writes <session>.json LAST (after the wavs), and needs no change to announce a finished interview."""

from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

import server.config as cfg
from server import db, ingest


def is_simulated(path) -> bool:
    try:
        return "simulated client" in json.loads(path.read_text(encoding="utf-8")).get("config", {}).get("tts", "")
    except Exception:  # noqa: BLE001
        return False


def claim_pending(started: float) -> str | None:
    """The live flow: the most recent 'created' row opened before this session ended (max concurrent sessions is 1)."""
    r = db.one("SELECT id FROM interviews WHERE status='created' AND is_sample=0 AND created_at <= ? ORDER BY created_at DESC LIMIT 1", started + 3600)
    return r["id"] if r else None


async def scan_once(since: float) -> list[str]:
    done = []
    for p in sorted(cfg.ENGINE_OUT.glob("26_interview_*.json"), key=lambda q: q.stat().st_mtime):
        if p.stat().st_mtime < since or time.time() - p.stat().st_mtime < 3:
            continue
        stem = p.stem
        if db.one("SELECT 1 FROM interviews WHERE engine_stem=?", stem) or is_simulated(p):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            started = data["mic_frames"][0][0] if data.get("mic_frames") else p.stat().st_mtime
            if (data["interview"].get("elapsed_s") or 0) < 5:  # a connect that never became an interview
                continue
            iid = ingest.ingest(stem, claim_pending(started))
            done.append(iid)
            logger.info(f"ingested session {stem} -> interview {iid[:8]}; scoring")
            asyncio.create_task(ingest.score(iid))
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the watcher
            logger.warning(f"could not ingest {stem}: {type(e).__name__}: {str(e)[:160]}")
    return done


async def run(poll_s: float = 3.0) -> None:
    since = time.time() - 5
    while True:
        await scan_once(since)
        await asyncio.sleep(poll_s)
