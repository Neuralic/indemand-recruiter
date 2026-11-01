"""Launch the product server: the frozen engine's runner (spike/26_live_interview.py, unchanged) plus the product REST API on the same app.

usage (from the project root, same flags as the engine):  python server/run.py -t webrtc
Env: WATCH_SESSIONS=1 (default) ingests finished live sessions; DASHBOARD_PASSCODE enables the dashboard; REAL_VOICE=1 for real TTS.
"""

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT / "spike")  # the engine resolves its paths from the spike folder
import server.config  # noqa: E402,F401  (sets sys.path for bot/ and spike/, loads .env)

from pipecat.runner.run import app  # noqa: E402

from server import api, watcher  # noqa: E402

api.install(app)
if os.environ.get("WATCH_SESSIONS", "1") == "1":
    import asyncio

    @app.on_event("startup")
    async def _watch() -> None:
        asyncio.create_task(watcher.run())

runpy.run_path(str(ROOT / "spike" / "26_live_interview.py"), run_name="__main__")
