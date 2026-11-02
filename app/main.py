"""FastAPI app: token-gated REST API + the built React frontend, same origin (no CORS).

Run locally:  .venv-app/Scripts/python.exe -m uvicorn app.main:app --port 8000
Hard rules: the browser gets only the Vapi PUBLIC key (from /api/config, after the access token is checked); the Vapi private key,
the Anthropic key and everything else stay here. Access is one unguessable token (?k=...) + noindex + the UI-SPEC footer.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
import yaml
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

import app.config as cfg
from app import scorecard, store, vapi
from role_config import RoleConfig, load_role

log = logging.getLogger("indemand")
_tasks: set[asyncio.Task] = set()
_inflight: set[str] = set()
_role_cache: dict[str, RoleConfig] = {}


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def role() -> RoleConfig:
    if cfg.LIVE_ROLE_ID not in _role_cache:
        _role_cache[cfg.LIVE_ROLE_ID] = load_role(cfg.LIVE_ROLE_ID)
    return _role_cache[cfg.LIVE_ROLE_ID]


def role_card() -> dict:
    y = yaml.safe_load((cfg.ROLES_DIR / f"{cfg.LIVE_ROLE_ID}.yaml").read_text(encoding="utf-8"))
    return {"id": y["id"], "title": y["title"], "location": y["location"], "summary": y["summary"],
            "selling_points": y.get("selling_points", []), "ontario": bool(y.get("ontario"))}


# ---- access: one unguessable token ----------------------------------------------------------------------------------------------------
def require_token(x_access_token: str | None = Header(default=None), k: str | None = Query(default=None)) -> None:
    if not cfg.ACCESS_TOKEN:  # fail closed: no token configured means nobody gets in
        raise HTTPException(404, "not found")
    given = x_access_token or k or ""
    if not hmac.compare_digest(given.encode(), cfg.ACCESS_TOKEN.encode()):
        raise HTTPException(404, "not found")  # same answer as an unknown route: reveals nothing


router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])  # every API route needs the token


# ---- config / role -----------------------------------------------------------------------------------------------------------------------
@router.get("/config")
def config():
    return {"role": role_card(), "booking_url": cfg.BOOKING_URL, "sample_label": cfg.SAMPLE_LABEL, "live_label": cfg.LIVE_LABEL,
            "vapi_public_key": cfg.VAPI_PUBLIC_KEY, "vapi_assistant_id": cfg.VAPI_ASSISTANT_ID,
            "voice_configured": bool(cfg.VAPI_PUBLIC_KEY and cfg.VAPI_ASSISTANT_ID), "paused": cfg.KILL_SWITCH,
            "max_call_seconds": cfg.MAX_CALL_SECONDS}


# ---- sessions (spend protection) + call lifecycle -------------------------------------------------------------------------------------
@router.post("/sessions")
def new_session():
    try:
        return {"session_id": store.open_session()}
    except store.SessionRefused as e:
        raise HTTPException(e.code, e.message) from e


class CallBody(BaseModel):
    call_id: str
    replaces: str | None = None  # a retry inside the same session: the call that failed to connect and is being replaced


@router.post("/sessions/{sid}/call")
def session_call(sid: str, body: CallBody):
    if not vapi.valid_call_id(body.call_id):
        raise HTTPException(400, "bad call id")
    s = store.one("SELECT * FROM sessions WHERE id = ?", sid)
    if not s:
        raise HTTPException(404, "unknown session")
    current = s["call_id"]
    if current and current != body.call_id:
        # The only way a session may change its call is to replace one that never connected (an automatic retry). It must be named, and it
        # must still be unscored, so a call that has a transcript or a scorecard can never be swapped out from under a session.
        if body.replaces != current:
            raise HTTPException(409, "this session already has a call")
        if not store.delete_unscored(current):
            raise HTTPException(409, "the earlier call can no longer be replaced")
    store.run("UPDATE sessions SET call_id = ? WHERE id = ?", body.call_id, sid)
    store.create_live(body.call_id)
    return {"interview_id": body.call_id}


@router.post("/sessions/{sid}/end")
async def session_end(sid: str, discard: bool = Query(default=False)):
    """The call is over (Avery hung up, the candidate ended it, or it dropped). Frees the slot and starts the scorecard.
    With discard=true (the call never connected) a call that is still unscored is forgotten instead of scored, leaving no ghost row."""
    s = store.close_session(sid)
    if not s:
        raise HTTPException(404, "unknown session")
    if s["call_id"]:
        if discard and store.delete_unscored(s["call_id"]):
            return {"interview_id": None, "discarded": True}
        start_processing(s["call_id"])
        return {"interview_id": s["call_id"]}
    return {"interview_id": None}


# ---- processing: Vapi transcript -> Claude scorecard ----------------------------------------------------------------------------------
def start_processing(call_id: str) -> None:
    """Score a call at most once. The database claim is what enforces it: a call that already has its scorecard, or is being scored
    right now, cannot be claimed, so repeated end events, reloads, retries and rebuilds are all no-ops for it."""
    if call_id in _inflight or not store.claim_for_scoring(call_id):
        return
    _inflight.add(call_id)
    spawn(process_live(call_id))


async def process_live(call_id: str) -> None:
    t0 = time.time()
    try:
        call = await vapi.fetch_finished_call(call_id)
        store.update(call_id, turns_json=json.dumps(call.turns, ensure_ascii=False), duration_s=call.duration_s, ended_at=call.ended_at,
                     ended_reason=call.ended_reason, has_recording=int(call.has_recording))
        res = await scorecard.generate(role(), call.turns, call.ended_reason, cfg.LIVE_LABEL)
        if res.ok:
            if not store.save_card(call_id, res.card, res.attempts, time.time() - t0):
                log.warning("scorecard for %s already existed; the new one was discarded", call_id)
        elif res.attempts == 0:
            store.mark_unfinished(call_id, "unusable", error=res.error)
        else:
            store.mark_unfinished(call_id, "failed", attempts=res.attempts, error=res.error)
            log.warning("scorecard failed for %s: %s", call_id, res.error)
    except vapi.NotOurCall:
        store.mark_unfinished(call_id, "failed", error="not one of this assistant's calls")
    except Exception as e:  # noqa: BLE001
        log.warning("processing %s failed: %s: %s", call_id, type(e).__name__, str(e)[:200])
        store.mark_unfinished(call_id, "failed", error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        _inflight.discard(call_id)


def adopt(call_id: str) -> bool:
    """A call id this server has no row for (the host restarted and lost its disk): rebuild it from Vapi. False if it can't be."""
    if not vapi.valid_call_id(call_id) or not (cfg.VAPI_PRIVATE_KEY and cfg.VAPI_ASSISTANT_ID):
        return False
    store.create_live(call_id, rebuilt=True)  # flagged, so the page can say this is a second scoring of the call
    start_processing(call_id)
    return True


# ---- interviews / scorecards ----------------------------------------------------------------------------------------------------------
def _row(interview_id: str):
    r = store.get(interview_id)
    if not r and not adopt(interview_id):
        raise HTTPException(404, "not found")
    return r or store.get(interview_id)


def _name(r) -> str:
    """What the page calls this interview: a simulated candidate's fictional name, or "Your interview" for a live session."""
    return r["candidate_name"] if r["kind"] == "sample" else "Your interview"


def _base(r) -> dict:
    return {"id": r["id"], "label": r["label"], "is_sample": r["kind"] == "sample", "candidate_name": _name(r),
            "role_id": r["role_id"], "duration_s": r["duration_s"], "has_recording": bool(r["has_recording"]) and r["kind"] == "live",
            "ended_reason": r["ended_reason"], "rebuilt": bool(r["rebuilt"])}


@router.get("/interviews/{interview_id}/scorecard")
async def get_scorecard(interview_id: str):
    r = _row(interview_id)
    base = _base(r)
    st = r["status"]
    if st == "ready":
        return {**base, "status": "ready", "scorecard": json.loads(r["card_json"]), "generated_in_s": r["generated_in_s"]}
    if st == "failed":
        return {**base, "status": "failed", "message": "Scorecard unavailable, retry", "attempts": r["attempts"]}
    if st == "unusable":
        no_audio = "did-not-receive-customer-audio" in (r["ended_reason"] or "")
        msg = ("Avery didn't receive any audio from your microphone, so the call ended. Turn off any VPN, check the browser's microphone permission, and try again."
               if no_audio else "There wasn't enough of a conversation to score. Please run the interview again.")
        return {**base, "status": "unusable", "message": msg}
    if st == "in-progress" and r["created_at"] < time.time() - cfg.MAX_CALL_SECONDS - 300:
        start_processing(interview_id)  # the browser never reported the end (tab closed): score it anyway
        return {**base, "status": "generating"}
    return {**base, "status": "waiting" if st == "in-progress" else "generating"}


@router.post("/interviews/{interview_id}/scorecard/retry")
async def retry_scorecard(interview_id: str):
    r = _row(interview_id)
    if r["kind"] != "live" or r["status"] not in ("failed", "unusable"):  # never for a call that has a scorecard or is being scored
        raise HTTPException(409, "scorecard is not in a failed state")
    start_processing(interview_id)
    return {"status": "generating"}


@router.get("/interviews/{interview_id}/transcript")
async def get_transcript(interview_id: str):
    r = _row(interview_id)
    from guardrails import redact_protected
    turns = json.loads(r["turns_json"] or "[]")
    return [{"speaker": t["speaker"], "text": redact_protected(t["text"])[0], "t_start": t["t_start"], "t_end": t["t_end"]} for t in turns]


@router.get("/interviews/{interview_id}/recording")
async def get_recording(interview_id: str, request: Request):
    """Proxy of the Vapi recording (fresh URL fetched with the private key), with Range support so the player can seek."""
    r = _row(interview_id)
    if r["kind"] != "live":
        raise HTTPException(404, "no recording")
    try:
        url = await vapi.recording_url(interview_id)
    except vapi.VapiError as e:
        raise HTTPException(404, "no recording") from e
    if not url:
        raise HTTPException(404, "no recording")
    headers = {"Range": request.headers["range"]} if "range" in request.headers else {}
    client = httpx.AsyncClient(timeout=30, follow_redirects=True)
    req = client.build_request("GET", url, headers=headers)
    up = await client.send(req, stream=True)
    if up.status_code >= 400:
        await up.aclose()
        await client.aclose()
        raise HTTPException(404, "no recording")
    out = {h: up.headers[h] for h in ("content-length", "content-range", "accept-ranges") if h in up.headers}
    out["cache-control"] = "private, max-age=600"

    async def body():
        try:
            async for chunk in up.aiter_bytes():
                yield chunk
        finally:
            await up.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=up.status_code, media_type=up.headers.get("content-type", "audio/wav"), headers=out)


# ---- dashboard -------------------------------------------------------------------------------------------------------------------------
@router.get("/dashboard")
async def dashboard(known: str | None = Query(default=None)):
    """Ranked shortlist. `known` = comma-separated call ids this browser ran (rebuilt from Vapi if the host lost its disk)."""
    for cid in [c for c in (known or "").split(",") if c][:10]:
        if not store.get(cid):
            adopt(cid)
    rows = store.q("SELECT * FROM interviews WHERE role_id = ? AND (kind = 'sample' OR status != 'in-progress' OR created_at > ?) "
                   "ORDER BY (status = 'ready') DESC, score DESC, created_at DESC", cfg.LIVE_ROLE_ID, time.time() - 3600)
    out, rank, unscored = [], 0, 0
    for r in rows:
        if r["kind"] == "live" and r["status"] != "ready":
            unscored += 1
            if unscored > 2:  # keep the table readable: only the two most recent unscored attempts (short, failed, still scoring)
                continue
        card = json.loads(r["card_json"]) if r["status"] == "ready" and r["card_json"] else None
        if card:
            rank += 1
        req = [{"id": c["id"], "status": c["status"]} for c in (card["knockouts"] if card else []) if c["required"]]
        out.append({"rank": rank if card else None, "id": r["id"], "candidate_name": _name(r), "label": r["label"],
                    "is_sample": r["kind"] == "sample", "sample_kind": r["sample_kind"], "status": r["status"], "score": r["score"],
                    "recommendation": r["recommendation"], "knockouts": req, "top_flag": (card["risks"][0] if card and card["risks"] else None),
                    "headline": card["headline"] if card else None, "date": r["ended_at"] or r["created_at"]})
    return {"role": role_card(), "candidates": out, "booking_url": cfg.BOOKING_URL}


# ---- app shell ---------------------------------------------------------------------------------------------------------------------------
def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("seeded %d simulated candidates", store.seed_samples())
        if not cfg.ACCESS_TOKEN:
            log.warning("ACCESS_TOKEN is not set: every request will be refused")
        yield

    app = FastAPI(title="IN DEMAND AI Recruiter prototype", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.include_router(router)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Referrer-Policy"] = "no-referrer"  # the token is in the URL: never leak it in a Referer header
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api") else resp.headers.get("Cache-Control", "no-cache"))
        return resp

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)  # HEAD too: uptime monitors default to it
    def healthz():
        return {"ok": True}

    dist = cfg.WEB_DIST
    index = dist / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        f = (dist / path).resolve()
        if path and f.is_file() and dist.resolve() in f.parents:
            return FileResponse(f, headers={"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else None)
        if index.exists():
            return FileResponse(index)
        return Response("frontend not built", status_code=503)

    return app


app = create_app()
