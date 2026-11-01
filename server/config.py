"""Paths and environment for the product backend (UI-SPEC.md section 2). Nothing here touches the frozen engine."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "bot", ROOT / "spike"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:  # same .env the engine uses (the stale Windows OPENAI_API_KEY differs from it, so override=True); keys are never printed
    from dotenv import load_dotenv
    load_dotenv(ROOT / "spike" / ".env", override=True)
    load_dotenv(ROOT / ".env", override=True)
except Exception:  # noqa: BLE001
    pass

DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA / "recruiter.db"))
RECORDINGS = Path(os.environ.get("RECORDINGS_DIR", DATA / "recordings"))
ENGINE_OUT = Path(os.environ.get("ENGINE_OUT_DIR", ROOT / "spike" / "out"))  # where the engine's dump() writes <session>.json + wavs
ROLES_DIR = ROOT / "roles"

LIVE_ROLES = ["5905"]  # only #5905 is live in v1
SAMPLE_LABEL = "Sample candidate (simulated)"
LIVE_LABEL = "Live demo session"

SCORECARD_MODEL = os.environ.get("SCORECARD_MODEL", "gpt-5.6-sol")
SCORECARD_FALLBACK = os.environ.get("SCORECARD_FALLBACK_MODEL", "gpt-5.6-terra")
SCORECARD_EFFORT = os.environ.get("SCORECARD_EFFORT", "medium")
SCORECARD_TIMEOUT_S = float(os.environ.get("SCORECARD_TIMEOUT_S", "70"))
SCORECARD_PROMPT_VERSION = "scorecard-v1"
SCORECARD_ATTEMPTS = 2

BOOKING_URL = os.environ.get("BOOKING_URL", "mailto:neuralicstudio@gmail.com?subject=Book%20a%2015-min%20call%20about%20the%20AI%20recruiter%20prototype")
DASHBOARD_PASSCODE = os.environ.get("DASHBOARD_PASSCODE", "")  # unset = the dashboard endpoints refuse everything

