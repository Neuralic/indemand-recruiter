"""SQLite storage (stdlib sqlite3; UI-SPEC.md section 2: SQLite plus wav files on disk, no Supabase for v1).

Rows mirror CLAUDE.md section 9 where it applies (interviews, transcript_turns, evidence, knockouts, recruiter_flags, scorecards).
Every interview row carries `is_sample` and `label` IN THE DATA, so a simulated candidate can never be shown as a real person.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from server.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
  id TEXT PRIMARY KEY,                -- unguessable (uuid4 hex): the scorecard link is the capability
  role_id TEXT NOT NULL,
  candidate_name TEXT NOT NULL,       -- what the dashboard shows ("Jordan Ellis" for a sample, "Live demo session" for a live run)
  label TEXT NOT NULL,                -- 'Sample candidate (simulated)' | 'Live demo session'
  is_sample INTEGER NOT NULL DEFAULT 0,
  sample_kind TEXT,                   -- strong | borderline | weak
  status TEXT NOT NULL,               -- created | recorded | scoring | ready | failed
  created_at REAL NOT NULL,
  started_at REAL, ended_at REAL, duration_s REAL,
  ended_reason TEXT,                  -- engine's: completed | off_topic | silence | human | error ...
  engine_stem TEXT UNIQUE,            -- the engine session this row was built from (spike/out/<stem>.json)
  recording_path TEXT,                -- the mix wav (both sides), copied under data/recordings
  engine_json TEXT,                   -- full audit record: stage log, probes, rejected quotes, config (prompt/model versions)
  engine_versions TEXT                -- json {realtime_model, evaluator, tts, prompt_version}
);
CREATE TABLE IF NOT EXISTS transcript_turns (
  interview_id TEXT NOT NULL REFERENCES interviews(id), idx INTEGER NOT NULL, speaker TEXT NOT NULL, text TEXT NOT NULL,
  t_start REAL NOT NULL, t_end REAL NOT NULL, PRIMARY KEY (interview_id, idx)
);
CREATE TABLE IF NOT EXISTS evidence (
  interview_id TEXT NOT NULL REFERENCES interviews(id), n INTEGER NOT NULL, step TEXT, competency_id TEXT, quote TEXT, assessment TEXT,
  forced_weak INTEGER, note TEXT, t REAL, PRIMARY KEY (interview_id, n)
);
CREATE TABLE IF NOT EXISTS knockouts (
  interview_id TEXT NOT NULL REFERENCES interviews(id), knockout_id TEXT NOT NULL, status TEXT, answer TEXT, quote TEXT,
  quote_verified INTEGER, required INTEGER, t REAL, PRIMARY KEY (interview_id, knockout_id)
);
CREATE TABLE IF NOT EXISTS recruiter_flags (
  interview_id TEXT NOT NULL REFERENCES interviews(id), n INTEGER NOT NULL, topic TEXT, reason TEXT, t REAL, PRIMARY KEY (interview_id, n)
);
CREATE TABLE IF NOT EXISTS scorecards (
  interview_id TEXT PRIMARY KEY REFERENCES interviews(id),
  status TEXT NOT NULL,               -- generating | ready | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  json TEXT,                          -- the validated scorecard (CLAUDE.md section 10 shape + logistics/deferred/versions)
  overall_score INTEGER, recommendation TEXT,
  model TEXT, prompt_version TEXT, error TEXT, generated_in_s REAL, updated_at REAL
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
    return _conn


def reset_for_tests(path) -> None:
    """Point the module at another database file (tests)."""
    global _conn
    import server.config as cfg
    from pathlib import Path
    if _conn is not None:
        _conn.close()
    _conn = None
    cfg.DB_PATH = Path(path)
    globals()["DB_PATH"] = Path(path)


@contextmanager
def tx():
    with _lock:
        c = conn()
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def q(sql: str, *args) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def one(sql: str, *args) -> sqlite3.Row | None:
    rows = q(sql, *args)
    return rows[0] if rows else None


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
