"""SQLite storage for the product (stdlib sqlite3). One row per interview, keyed by the Vapi call id (live) or a fixed id (sample).

The host's disk may be ephemeral (Render free): everything live can be rebuilt from the Vapi call id, and the samples are re-seeded
from app/seed/samples.json at startup. `is_sample` and `label` live IN THE DATA so a simulated candidate can never be shown as a person.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import app.config as cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
  id TEXT PRIMARY KEY,                 -- Vapi call id (live) | 'sample-<kind>' (simulated)
  kind TEXT NOT NULL,                  -- live | sample
  role_id TEXT NOT NULL,
  candidate_name TEXT NOT NULL,
  label TEXT NOT NULL,                 -- 'Live demo session' | 'Sample candidate (simulated)'
  sample_kind TEXT,                    -- strong | borderline | weak
  status TEXT NOT NULL,                -- in-progress | processing | ready | failed | unusable
  created_at REAL NOT NULL, ended_at REAL, duration_s REAL, ended_reason TEXT,
  has_recording INTEGER NOT NULL DEFAULT 0,
  turns_json TEXT, card_json TEXT, score INTEGER, recommendation TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT, generated_in_s REAL, updated_at REAL,
  rebuilt INTEGER NOT NULL DEFAULT 0   -- 1 = this server had no record of the call (e.g. it restarted) and scored it again from Vapi
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, call_id TEXT, closed_at REAL
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(cfg.DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        if "rebuilt" not in {r["name"] for r in _conn.execute("PRAGMA table_info(interviews)")}:  # database made before this column existed
            _conn.execute("ALTER TABLE interviews ADD COLUMN rebuilt INTEGER NOT NULL DEFAULT 0")
    return _conn


def reset(path=None) -> None:
    """Close and forget the connection (tests point DB_PATH elsewhere first)."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None
    if path is not None:
        from pathlib import Path
        cfg.DB_PATH = Path(path)


def q(sql: str, *args) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def one(sql: str, *args) -> sqlite3.Row | None:
    rows = q(sql, *args)
    return rows[0] if rows else None


def run(sql: str, *args) -> None:
    with _lock:
        conn().execute(sql, args)


@contextmanager
def tx():
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


# ---- sessions: spend protection (kill switch, concurrency, daily cap) ---------------------------------------------------------------
class SessionRefused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _utc_midnight(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def open_session(now: float | None = None) -> str:
    now = now or time.time()
    if cfg.KILL_SWITCH:
        raise SessionRefused(503, "Interviews are paused right now. Please try again later.")
    with tx() as c:
        today = c.execute("SELECT COUNT(*) FROM sessions WHERE created_at >= ?", (_utc_midnight(now),)).fetchone()[0]
        if today >= cfg.DAILY_SESSION_CAP:
            raise SessionRefused(429, "The daily limit for demo interviews has been reached. Please try again tomorrow.")
        active = c.execute("SELECT COUNT(*) FROM sessions WHERE closed_at IS NULL AND created_at > ?",
                           (now - cfg.MAX_CALL_SECONDS - 120,)).fetchone()[0]
        if active >= cfg.MAX_CONCURRENT:
            raise SessionRefused(429, "Another interview is in progress. Please try again in a few minutes.")
        sid = uuid.uuid4().hex
        c.execute("INSERT INTO sessions (id, created_at) VALUES (?, ?)", (sid, now))
    return sid


def close_session(sid: str) -> sqlite3.Row | None:
    run("UPDATE sessions SET closed_at = COALESCE(closed_at, ?) WHERE id = ?", time.time(), sid)
    return one("SELECT * FROM sessions WHERE id = ?", sid)


# ---- interviews ---------------------------------------------------------------------------------------------------------------------
def create_live(call_id: str, name: str = "", role_id: str | None = None, rebuilt: bool = False) -> None:
    now = time.time()
    run("INSERT OR IGNORE INTO interviews (id, kind, role_id, candidate_name, label, status, created_at, updated_at, rebuilt) "
        "VALUES (?, 'live', ?, ?, ?, 'in-progress', ?, ?, ?)",
        call_id, role_id or cfg.LIVE_ROLE_ID, name or cfg.LIVE_LABEL, cfg.LIVE_LABEL, now, now, int(rebuilt))


def update(interview_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    run(f"UPDATE interviews SET {cols} WHERE id = ?", *fields.values(), interview_id)


def get(interview_id: str) -> sqlite3.Row | None:
    return one("SELECT * FROM interviews WHERE id = ?", interview_id)


STALE_PROCESSING_S = 600  # a 'processing' row this old was left by a crashed worker and may be claimed again


def claim_for_scoring(call_id: str) -> bool:
    """Atomically move a live call to 'processing'. True only if the caller may now score it. A call that is 'ready' (or is being scored
    right now) can never be claimed, so a repeated end event, a reload, a retry or a rebuild cannot start a second scoring of it."""
    now = time.time()
    with _lock:
        cur = conn().execute(
            "UPDATE interviews SET status = 'processing', error = NULL, updated_at = ? WHERE id = ? AND kind = 'live' AND "
            "(status IN ('in-progress', 'failed', 'unusable') OR (status = 'processing' AND updated_at < ?))",
            (now, call_id, now - STALE_PROCESSING_S))
        return cur.rowcount == 1


def save_card(interview_id: str, card: dict, attempts: int, seconds: float) -> bool:
    """Write the scorecard ONCE. Returns False (and changes nothing) if the call already has one."""
    with _lock:
        cur = conn().execute(
            "UPDATE interviews SET status = 'ready', card_json = ?, score = ?, recommendation = ?, attempts = ?, error = NULL, generated_in_s = ?, "
            "updated_at = ? WHERE id = ? AND status != 'ready'",
            (json.dumps(card, ensure_ascii=False), card["overall_score"], card["recommendation"], attempts, round(seconds, 1), time.time(), interview_id))
        return cur.rowcount == 1


def mark_unfinished(interview_id: str, status: str, **fields) -> None:
    """Record a failed / unusable outcome, but never touch a call that already has its scorecard."""
    fields.update(status=status, updated_at=time.time())
    cols = ", ".join(f"{k} = ?" for k in fields)
    run(f"UPDATE interviews SET {cols} WHERE id = ? AND status != 'ready'", *fields.values(), interview_id)


def seed_samples() -> int:
    """Load the committed simulated candidates (app/seed/samples.json). Idempotent. Returns how many are present."""
    if not cfg.SEED_FILE.exists():
        return 0
    data = json.loads(cfg.SEED_FILE.read_text(encoding="utf-8"))
    for s in data.get("samples", []):
        if s.get("label") != cfg.SAMPLE_LABEL:  # refuse to load anything not labelled as simulated
            raise ValueError(f"sample {s.get('id')} is not labelled {cfg.SAMPLE_LABEL!r}")
        card = s["card"]
        run("INSERT OR REPLACE INTO interviews (id, kind, role_id, candidate_name, label, sample_kind, status, created_at, ended_at, "
            "duration_s, ended_reason, has_recording, turns_json, card_json, score, recommendation, attempts, generated_in_s, updated_at) "
            "VALUES (?, 'sample', ?, ?, ?, ?, 'ready', ?, ?, ?, ?, 0, ?, ?, ?, ?, 1, ?, ?)",
            s["id"], s.get("role_id", cfg.LIVE_ROLE_ID), s["candidate_name"], cfg.SAMPLE_LABEL, s["sample_kind"],
            s.get("created_at", 0), s.get("created_at", 0), s.get("duration_s"), "simulated",
            json.dumps(s["turns"], ensure_ascii=False), json.dumps(card, ensure_ascii=False), card["overall_score"],
            card["recommendation"], s.get("generated_in_s"), time.time())
    return len(data.get("samples", []))
