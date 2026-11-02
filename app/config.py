"""Environment and paths for the product backend (Vapi voice + Claude scorecard).

Deliberately does NOT read OPENAI_API_KEY or load spike/.env: nothing on the product path may need an OpenAI key.
The frozen Pipecat engine (bot/, spike/, server/) stays in the repo and is never imported here, except three pure modules
(role_config, guardrails, quote_validation) which have no pipecat/openai imports.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)  # real env vars (Render) win over a local .env
except Exception:  # noqa: BLE001
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# --- secrets (server side only; only VAPI_PUBLIC_KEY ever reaches the browser, and only after the access token is checked) ---
VAPI_PUBLIC_KEY = os.environ.get("VAPI_PUBLIC_KEY", "")
VAPI_PRIVATE_KEY = os.environ.get("VAPI_PRIVATE_KEY", "")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID", "")
VAPI_API_BASE = os.environ.get("VAPI_API_BASE", "https://api.vapi.ai")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
ANTHROPIC_FALLBACK_MODEL = os.environ.get("ANTHROPIC_FALLBACK_MODEL", "")  # blank = retry the same model
SCORECARD_EFFORT = os.environ.get("SCORECARD_EFFORT", "medium")
SCORECARD_TIMEOUT_S = float(os.environ.get("SCORECARD_TIMEOUT_S", "75"))
SCORECARD_ATTEMPTS = 2
SCORECARD_PROMPT_VERSION = "scorecard-claude-v3"

# --- access: one unguessable token in the URL (?k=...) ---
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")

# --- spend protection (UI-SPEC section 3) ---
KILL_SWITCH = _flag("KILL_SWITCH")
MAX_CONCURRENT = _int("MAX_CONCURRENT_SESSIONS", 1)
DAILY_SESSION_CAP = _int("DAILY_SESSION_CAP", 20)
MAX_CALL_SECONDS = _int("MAX_CALL_SECONDS", 16 * 60)  # the assistant is capped at 16 min in Vapi; a slot is freed after this + slack

BOOKING_URL = os.environ.get(
    "BOOKING_URL",
    "mailto:neuralicstudio@gmail.com?subject=Book%20a%2015-min%20call%20about%20the%20AI%20recruiter%20prototype")

DATA = Path(os.environ.get("DATA_DIR", ROOT / "data" / "app"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA / "app.db"))
ROLES_DIR = ROOT / "roles"
SEED_FILE = Path(os.environ.get("SEED_FILE", ROOT / "app" / "seed" / "samples.json"))
WEB_DIST = Path(os.environ.get("WEB_DIST", ROOT / "web" / "dist"))

LIVE_ROLE_ID = "5905"  # only #5905 is live in v1
SAMPLE_LABEL = "Sample candidate (simulated)"
LIVE_LABEL = "Live demo session"
