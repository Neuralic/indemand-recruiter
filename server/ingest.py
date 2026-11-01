"""Engine session -> product storage. Reads the frozen engine's own files (spike/out/<stem>.json + <stem>_mix.wav) and never modifies them."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import server.config as cfg
from role_config import load_role
from server import db, scorecard, session_data


def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def create_interview(role_id: str = "5905", candidate_name: str = cfg.LIVE_LABEL, label: str = cfg.LIVE_LABEL, is_sample: bool = False,
                     sample_kind: str | None = None) -> str:
    iid = db.new_id()
    with db.tx() as c:
        c.execute("INSERT INTO interviews (id, role_id, candidate_name, label, is_sample, sample_kind, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (iid, role_id, candidate_name, label, int(is_sample), sample_kind, "created", db.now()))
    return iid


def ingest(stem: str, interview_id: str | None = None, *, role_id: str = "5905", candidate_name: str | None = None, label: str | None = None,
           is_sample: bool = False, sample_kind: str | None = None) -> str:
    """Store one finished engine session. With `interview_id` the existing 'created' row is filled in (the live flow); otherwise a row is made."""
    json_path = cfg.ENGINE_OUT / f"{stem}.json"
    if db.one("SELECT id FROM interviews WHERE engine_stem=?", stem):
        return db.one("SELECT id FROM interviews WHERE engine_stem=?", stem)["id"]
    s = session_data.load(json_path)
    role = load_role(role_id)
    iv = s["interview"]
    turns = session_data.build_transcript(s)
    if interview_id is None:
        interview_id = create_interview(role_id, candidate_name or cfg.LIVE_LABEL, label or cfg.LIVE_LABEL, is_sample, sample_kind)
    elif candidate_name or label:
        with db.tx() as c:
            c.execute("UPDATE interviews SET candidate_name=COALESCE(?,candidate_name), label=COALESCE(?,label) WHERE id=?", (candidate_name, label, interview_id))
    started = s["mic_frames"][0][0] if s.get("mic_frames") else s["events"][0]["t"]
    rec_dst = None
    mix = session_data.recording_paths(json_path)["mix"]
    if mix.exists():
        cfg.RECORDINGS.mkdir(parents=True, exist_ok=True)
        rec_dst = cfg.RECORDINGS / f"{interview_id}.wav"
        shutil.copyfile(mix, rec_dst)
    conf = s.get("config", {})
    versions = {"realtime_model": conf.get("realtime_model"), "evaluator": conf.get("evaluator"), "engine_prompt": conf.get("prompt_version"),
                "tts": "stub" if "STUB" in str(conf.get("tts")) else str(conf.get("tts"))[:40]}
    with db.tx() as c:
        c.execute("UPDATE interviews SET status='recorded', started_at=?, ended_at=?, duration_s=?, ended_reason=?, engine_stem=?, recording_path=?, "
                  "engine_json=?, engine_versions=? WHERE id=?",
                  (started, started + session_data.duration_s(s), session_data.duration_s(s), iv.get("ended"), stem,
                   str(rec_dst) if rec_dst else None, db.dumps({k: iv.get(k) for k in ("stage_log", "probe_used", "rejected_quotes", "placeholders",
                                                                                          "protected_info_volunteered_count", "elapsed_s", "ended")}
                                                                | {"config": conf}), db.dumps(versions), interview_id))
        for t in turns:
            c.execute("INSERT INTO transcript_turns VALUES (?,?,?,?,?,?)", (interview_id, t.idx, t.speaker, t.text, t.t_start, t.t_end))
        for n, e in enumerate(iv.get("evidence", [])):
            c.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?)", (interview_id, n, e["step"], e["competency_id"], e["quote"], e["assessment"],
                                                                         int(e["assessment_forced_weak"]), e["note"], e["t"]))
        ko = iv.get("knockouts", {})
        for k in role.knockouts:
            r = ko.get(k.id)
            c.execute("INSERT INTO knockouts VALUES (?,?,?,?,?,?,?,?)",
                      (interview_id, k.id, r["status"] if r else "unclear", (r or {}).get("answer") or ("" if r else "Not reached in this interview"),
                       (r or {}).get("quote", ""), int(bool((r or {}).get("quote_verified"))), int(k.required), (r or {}).get("t")))
        for n, f in enumerate(iv.get("flags", [])):
            c.execute("INSERT INTO recruiter_flags VALUES (?,?,?,?,?)", (interview_id, n, f["topic"], f["reason"], f["t"]))
    return interview_id


def load_inputs(interview_id: str) -> dict:
    iv = db.one("SELECT * FROM interviews WHERE id=?", interview_id)
    return {
        "iv": _row(iv),
        "turns": [_row(r) for r in db.q("SELECT * FROM transcript_turns WHERE interview_id=? ORDER BY idx", interview_id)],
        "evidence": [_row(r) for r in db.q("SELECT * FROM evidence WHERE interview_id=? ORDER BY n", interview_id)],
        "knockouts": [_row(r) for r in db.q("SELECT * FROM knockouts WHERE interview_id=?", interview_id)],
        "flags": [_row(r) for r in db.q("SELECT * FROM recruiter_flags WHERE interview_id=? ORDER BY n", interview_id)],
    }


async def score(interview_id: str, client=None) -> bool:
    """Generate (or regenerate) the scorecard. Never raises; the outcome is in the scorecards row."""
    t0 = time.time()
    d = load_inputs(interview_id)
    role = load_role(d["iv"]["role_id"])
    for k in d["knockouts"]:
        k["question"] = next((x.question for x in role.knockouts if x.id == k["knockout_id"]), "")
    with db.tx() as c:
        c.execute("INSERT INTO scorecards (interview_id, status, attempts, updated_at) VALUES (?, 'generating', 0, ?) "
                  "ON CONFLICT(interview_id) DO UPDATE SET status='generating', attempts=0, error=NULL, updated_at=excluded.updated_at", (interview_id, time.time()))
        c.execute("UPDATE interviews SET status='scoring' WHERE id=?", (interview_id,))

    def on_attempt(n: int, model: str) -> None:
        with db.tx() as c:
            c.execute("UPDATE scorecards SET attempts=?, model=?, updated_at=? WHERE interview_id=?", (n, model, time.time(), interview_id))

    try:
        versions = __import__("json").loads(d["iv"]["engine_versions"] or "{}")
        res = await scorecard.generate(role, d["turns"], d["evidence"], d["knockouts"], d["flags"], d["iv"]["ended_reason"] or "", versions,
                                       d["iv"]["label"], client=client, on_attempt=on_attempt)
    except Exception as e:  # noqa: BLE001
        res = scorecard.Result(False, error=f"{type(e).__name__}: {str(e)[:200]}", attempts=cfg.SCORECARD_ATTEMPTS)
    with db.tx() as c:
        if res.ok:
            card = res.card
            c.execute("UPDATE scorecards SET status='ready', attempts=?, json=?, overall_score=?, recommendation=?, model=?, prompt_version=?, error=NULL, "
                      "generated_in_s=?, updated_at=? WHERE interview_id=?",
                      (res.attempts, db.dumps(card), card["overall_score"], card["recommendation"], res.model, cfg.SCORECARD_PROMPT_VERSION,
                       round(time.time() - t0, 1), time.time(), interview_id))
            c.execute("UPDATE interviews SET status='ready' WHERE id=?", (interview_id,))
        else:
            c.execute("UPDATE scorecards SET status='failed', attempts=?, error=?, generated_in_s=?, updated_at=? WHERE interview_id=?",
                      (res.attempts, res.error, round(time.time() - t0, 1), time.time(), interview_id))
            c.execute("UPDATE interviews SET status='failed' WHERE id=?", (interview_id,))
    return res.ok
