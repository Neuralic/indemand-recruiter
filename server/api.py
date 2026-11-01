"""REST endpoints (UI-SPEC.md section 2). Mounted on the engine runner's FastAPI app by server/run.py: same origin, no CORS."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time

import yaml
from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import server.config as cfg
from server import db, ingest

router = APIRouter(prefix="/api")
_tasks: set[asyncio.Task] = set()
COOKIE = "dash"


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# ---- roles ------------------------------------------------------------------------------------------------------------------------
def role_card(role_id: str) -> dict:
    if role_id not in cfg.LIVE_ROLES:
        raise HTTPException(404, "unknown role")
    y = yaml.safe_load((cfg.ROLES_DIR / f"{role_id}.yaml").read_text(encoding="utf-8"))
    return {"id": y["id"], "title": y["title"], "location": y["location"], "summary": y["summary"], "selling_points": y.get("selling_points", []),
            "ontario": bool(y.get("ontario"))}


@router.get("/roles")
def roles():
    return [role_card(r) for r in cfg.LIVE_ROLES]


@router.get("/roles/{role_id}")
def role(role_id: str):
    return role_card(role_id)


@router.get("/config")
def public_config():
    return {"booking_url": cfg.BOOKING_URL, "label_sample": cfg.SAMPLE_LABEL}


# ---- interviews and scorecards (the unguessable id is the capability) ----------------------------------------------------------------
class NewInterview(BaseModel):
    role_id: str = "5905"


@router.post("/interviews")
def create(body: NewInterview):
    role_card(body.role_id)
    return {"interview_id": ingest.create_interview(body.role_id)}


def _iv(interview_id: str):
    r = db.one("SELECT * FROM interviews WHERE id=?", interview_id)
    if not r:
        raise HTTPException(404, "not found")
    return r


@router.get("/interviews/{interview_id}")
def interview(interview_id: str):
    r = _iv(interview_id)
    return {"id": r["id"], "role_id": r["role_id"], "status": r["status"], "label": r["label"], "is_sample": bool(r["is_sample"]),
            "candidate_name": r["candidate_name"], "duration_s": r["duration_s"], "ended_reason": r["ended_reason"],
            "has_recording": bool(r["recording_path"])}


@router.get("/interviews/{interview_id}/scorecard")
def get_scorecard(interview_id: str):
    r = _iv(interview_id)
    sc = db.one("SELECT * FROM scorecards WHERE interview_id=?", interview_id)
    base = {"id": r["id"], "label": r["label"], "is_sample": bool(r["is_sample"]), "candidate_name": r["candidate_name"],
            "role_id": r["role_id"], "has_recording": bool(r["recording_path"]), "duration_s": r["duration_s"]}
    if not sc:  # nothing recorded yet (the interview is still running, or its recording is being saved)
        return {**base, "status": "pending"}
    if sc["status"] == "ready":
        return {**base, "status": "ready", "scorecard": json.loads(sc["json"]), "generated_in_s": sc["generated_in_s"]}
    if sc["status"] == "failed":
        return {**base, "status": "failed", "message": "Scorecard unavailable, retry", "attempts": sc["attempts"]}
    return {**base, "status": "generating", "attempts": sc["attempts"]}


@router.post("/interviews/{interview_id}/scorecard/retry")
def retry_scorecard(interview_id: str):
    r = _iv(interview_id)
    sc = db.one("SELECT status FROM scorecards WHERE interview_id=?", interview_id)
    if r["status"] not in ("failed", "recorded") and not (sc and sc["status"] == "failed"):
        raise HTTPException(409, "scorecard is not in a failed state")
    spawn(ingest.score(interview_id))
    return {"status": "generating"}


@router.get("/interviews/{interview_id}/transcript")
def transcript(interview_id: str):
    _iv(interview_id)
    return [{"idx": t["idx"], "speaker": t["speaker"], "text": t["text"], "t_start": t["t_start"], "t_end": t["t_end"]}
            for t in db.q("SELECT * FROM transcript_turns WHERE interview_id=? ORDER BY idx", interview_id)]


@router.get("/interviews/{interview_id}/recording")
def recording(interview_id: str):
    r = _iv(interview_id)
    if not r["recording_path"] or not os.path.exists(r["recording_path"]):
        raise HTTPException(404, "no recording")
    return FileResponse(r["recording_path"], media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})  # Range supported


# ---- dashboard (passcode + cookie) ---------------------------------------------------------------------------------------------------
def _token() -> str:
    secret = os.environ.get("APP_SECRET") or hashlib.sha256(("indemand-dashboard|" + cfg.DASHBOARD_PASSCODE).encode()).hexdigest()
    return hmac.new(secret.encode(), b"dashboard-v1", hashlib.sha256).hexdigest()


def require_dashboard(dash: str | None = Cookie(default=None)) -> None:
    if not cfg.DASHBOARD_PASSCODE:
        raise HTTPException(503, "dashboard passcode is not configured")
    if not dash or not hmac.compare_digest(dash, _token()):
        raise HTTPException(401, "passcode required")


class Login(BaseModel):
    passcode: str


_fails: dict[str, list[float]] = {}


@router.post("/dashboard/login")
def login(body: Login, request: Request, response: Response):
    if not cfg.DASHBOARD_PASSCODE:
        raise HTTPException(503, "dashboard passcode is not configured")
    ip = request.client.host if request.client else "?"
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 60]
    if len(recent) >= 8:
        raise HTTPException(429, "too many attempts, wait a minute")
    if not hmac.compare_digest(body.passcode.encode(), cfg.DASHBOARD_PASSCODE.encode()):
        _fails[ip] = recent + [time.time()]
        raise HTTPException(401, "wrong passcode")
    response.set_cookie(COOKIE, _token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14, secure=request.url.scheme == "https")
    return {"ok": True}


@router.post("/dashboard/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/dashboard/roles/{role_id}")
def dashboard(role_id: str, dash: str | None = Cookie(default=None)):
    require_dashboard(dash)
    rc = role_card(role_id)
    rows = db.q("SELECT i.*, s.status AS sc_status, s.json AS sc_json, s.overall_score AS score, s.recommendation AS rec FROM interviews i "
                "LEFT JOIN scorecards s ON s.interview_id=i.id WHERE i.role_id=? AND i.status != 'created' "
                "ORDER BY (s.status='ready') DESC, s.overall_score DESC, i.created_at DESC", role_id)
    out, rank = [], 0
    for r in rows:
        card = json.loads(r["sc_json"]) if r["sc_status"] == "ready" and r["sc_json"] else None
        ko = [{"id": k["knockout_id"], "status": k["status"]} for k in db.q(
            "SELECT * FROM knockouts WHERE interview_id=? AND required=1 ORDER BY rowid", r["id"])]
        flag = (card["risks"][0] if card and card["risks"] else None) or next(iter(
            f["reason"] for f in db.q("SELECT * FROM recruiter_flags WHERE interview_id=? ORDER BY n", r["id"])), None)
        if card:
            rank += 1
        out.append({"rank": rank if card else None, "id": r["id"], "candidate_name": r["candidate_name"], "label": r["label"],
                    "is_sample": bool(r["is_sample"]), "sample_kind": r["sample_kind"], "status": r["sc_status"] or r["status"], "score": r["score"],
                    "recommendation": r["rec"], "knockouts": ko, "top_flag": flag, "headline": card["headline"] if card else None,
                    "date": r["ended_at"] or r["created_at"]})
    return {"role": rc, "candidates": out}


# ---- misc ----------------------------------------------------------------------------------------------------------------------------
def install(app) -> None:
    """Register everything on the runner's FastAPI app. noindex everywhere (UI-SPEC section 3)."""
    app.include_router(router)

    @app.middleware("http")
    async def noindex(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")
