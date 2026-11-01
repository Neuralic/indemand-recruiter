"""Backend tests: scorecard validation/assembly, the two-attempt worker (fake model client), ingest from an engine-format session file,
and the REST API (labels in the data, passcode, ranking, recording). No network, no keys, no engine process."""

import asyncio
import json
import struct
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import server.config as cfg  # noqa: E402
from role_config import load_role  # noqa: E402
from server import api, db, ingest, scorecard  # noqa: E402

ROLE = load_role("5905")
TMP = Path(tempfile.mkdtemp())
db.reset_for_tests(TMP / "t.db")
cfg.RECORDINGS, cfg.ENGINE_OUT, cfg.DASHBOARD_PASSCODE = TMP / "rec", TMP / "out", "open-sesame"
cfg.ENGINE_OUT.mkdir()


def turns():
    return [
        {"idx": 0, "speaker": "avery", "text": "Tell me about the industrial electrical power systems you've been responsible for.", "t_start": 10.0, "t_end": 14.0},
        {"idx": 1, "speaker": "candidate", "text": "At a gas plant I designed the three-phase power for a new compressor building.", "t_start": 20.0, "t_end": 26.0},
        {"idx": 2, "speaker": "candidate", "text": "I programmed PLC and built the HMI screens on last [unclear] and cut start-up faults by half.", "t_start": 40.0, "t_end": 47.0},
    ]


def good_output(**over):
    d = {"overall_score": 82, "recommendation": "Submit", "headline": "Strong power systems lead.",
         "competencies": [{"id": c.id, "score": 4, "confidence": "high", "quotes": [], "rationale": "Specific example given."} for c in ROLE.competencies],
         "strengths": ["a", "b"], "risks": ["x", "y"], "verify_on_call": ["one", "two", "three"], "candidate_questions_to_answer": []}
    d.update(over)
    return d


def kos(**status):
    return [{"knockout_id": k.id, "status": status.get(k.id, "pass"), "required": k.required, "answer": "yes", "quote": "yes I am", "quote_verified": 1,
             "t": 1.0, "question": k.question} for k in ROLE.knockouts]


def test_validation_rejects_out_of_range_and_incomplete_output():
    for bad in ({"overall_score": 101}, {"overall_score": -1}, {"verify_on_call": ["only", "two"]}, {"headline": ""}):
        try:
            scorecard.validate_model_output(good_output(**bad), ROLE)
            raise AssertionError(bad)
        except scorecard.ScorecardError:
            pass
    d = good_output()
    d["competencies"][0]["score"] = 6
    try:
        scorecard.validate_model_output(d, ROLE)
        raise AssertionError("score 6")
    except scorecard.ScorecardError:
        pass
    scorecard.validate_model_output(good_output(), ROLE)


def test_recommendation_is_rule_based_and_a_failed_required_knockout_caps_at_hold():
    assert scorecard.recommendation_for(90, kos())[0] == "Submit"
    assert scorecard.recommendation_for(60, kos())[0] == "Hold" and scorecard.recommendation_for(30, kos())[0] == "Pass"
    rec, why = scorecard.recommendation_for(90, kos(pe_apega="fail"))
    assert rec == "Hold" and "pe_apega" in why


def test_quotes_are_verified_timed_and_unverifiable_ones_dropped():
    ev, dropped = scorecard.verify_quotes(["I designed the three-phase power for a new compressor building",
                                           "I cut downtime by forty percent across three sites"], turns())
    assert dropped == 1 and len(ev) == 1 and ev[0]["verified"] and ev[0]["t"] == 20.0
    # a quote spanning a protected-ground mishear keeps only the redacted transcript words
    ev, _ = scorecard.verify_quotes(["I programmed PLC and built the HMI screens on last kid and cut start-up faults by half"], turns())
    assert ev and "kid" not in ev[0]["quote"].lower() and "[unclear]" in ev[0]["quote"] and ev[0]["t"] == 40.0


def test_assembled_scorecard_marks_competencies_without_a_verified_quote_and_strips_protected_words():
    d = good_output(headline="Solid lead, mentions his family size", risks=["Has kids at home"])
    d["competencies"][0]["quotes"] = ["At a gas plant I designed the three-phase power for a new compressor building"]
    d["competencies"][1]["quotes"] = ["a made-up sentence the candidate never said at all"]
    card = scorecard.assemble(d, ROLE, turns(), [], kos(), [{"topic": "candidate asked about the client", "reason": "Who is the client?", "t": 5}],
                              {"x": 1}, cfg.SAMPLE_LABEL)
    c0, c1 = card["competencies"][0], card["competencies"][1]
    assert c0["evidence"][0]["t"] == 20.0 and c0["confidence"] == "high"
    assert c1["evidence"] == [] and c1["confidence"] == "low" and "no verified quote" in c1["evidence_note"]
    assert card["quote_check"] == {"proposed": 2, "dropped": 1}
    blob = json.dumps(card).lower()
    assert "family" not in blob and "kids" not in blob
    assert card["label"] == cfg.SAMPLE_LABEL and card["deferred_to_stefanie"][0]["topic"] == "client"
    assert card["knockouts_required_passed"] == card["knockouts_required"] == 4


class FakeClient:
    def __init__(self, outputs):
        self.outputs, self.models = list(outputs), []
        self.responses = self

    async def create(self, **kw):
        self.models.append(kw["model"])
        o = self.outputs.pop(0)
        if isinstance(o, Exception):
            raise o

        class R:
            output_text = json.dumps(o)
        return R()


def run_generate(client):
    return asyncio.run(scorecard.generate(ROLE, turns(), [], kos(), [], "completed", {}, cfg.SAMPLE_LABEL, client=client))


def test_worker_falls_back_to_the_second_model_after_an_invalid_first_attempt():
    c = FakeClient([good_output(overall_score=140), good_output()])
    r = run_generate(c)
    assert r.ok and r.attempts == 2 and c.models == [cfg.SCORECARD_MODEL, cfg.SCORECARD_FALLBACK] and r.card["overall_score"] == 82


def test_worker_gives_up_cleanly_after_two_failed_attempts():
    r = run_generate(FakeClient([TimeoutError("slow"), good_output(verify_on_call=[])]))
    assert not r.ok and r.attempts == 2 and r.card is None and r.error


# ---- ingest + API --------------------------------------------------------------------------------------------------------------------
def write_session(stem: str, text: str = "At a gas plant I designed the three-phase power for a new compressor building.") -> None:
    t0 = 1000.0
    ev = [
        {"tap": "out", "kind": "BotStartedSpeakingFrame", "t": t0 + 0.5},
        {"tap": "openai_vad", "kind": "speech_started", "t": t0 + 20.0, "audio_ms": 20000},
        {"tap": "openai_vad", "kind": "speech_stopped", "t": t0 + 26.0, "audio_ms": 26000},
        {"tap": "user_text", "kind": "kept", "t": t0 + 26.6, "raw": text, "clean": text, "echo": [], "audio_s": 6.0},
        {"tap": "director", "kind": "ack", "t": t0 + 27.0, "text": "Got it.", "raw": "Got it.", "reason": "ok"},
    ]
    iv = {"role": "5905", "ended": "completed", "elapsed_s": 60.0, "stage_log": [], "probe_used": [], "rejected_quotes": [], "placeholders": [],
          "protected_info_volunteered_count": 0, "flags": [{"t": 50, "topic": "candidate asked about the client", "reason": "Who is the client?"}],
          "evidence": [{"t": 30, "step": "technical_electrical", "competency_id": "technical_electrical", "quote": "I designed the three-phase power",
                        "assessment": "strong", "assessment_forced_weak": False, "note": "n", "match_score": 1.0}],
          "knockouts": {"work_auth": {"status": "pass", "answer": "yes", "quote": "Yes I am", "quote_verified": True, "t": 40}}}
    (cfg.ENGINE_OUT / f"{stem}.json").write_text(json.dumps({
        "events": ev, "mic_frames": [[t0, 960]], "transcript": [["bot", "Hi Jordan, I'm Avery."]], "interview": iv,
        "config": {"realtime_model": "gpt-realtime-2.1", "evaluator": "gpt-5.6-terra/none", "tts": "STUB voice (no credits; simulated client)",
                   "prompt_version": "ack-persona-v2"}}), encoding="utf-8")
    with wave.open(str(cfg.ENGINE_OUT / f"{stem}_mix.wav"), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(24000)
        w.writeframes(struct.pack("<h", 0) * 24000)


def test_ingest_stores_a_labelled_sample_with_a_timed_transcript_and_recording():
    write_session("26_interview_test_1")
    iid = ingest.ingest("26_interview_test_1", candidate_name="Taylor Brooks", label=cfg.SAMPLE_LABEL, is_sample=True, sample_kind="strong")
    r = db.one("SELECT * FROM interviews WHERE id=?", iid)
    assert r["is_sample"] == 1 and r["label"] == "Sample candidate (simulated)" and r["status"] == "recorded" and Path(r["recording_path"]).exists()
    tt = db.q("SELECT * FROM transcript_turns WHERE interview_id=? ORDER BY idx", iid)
    cand = [t for t in tt if t["speaker"] == "candidate"][0]
    assert cand["t_start"] == 20.0 and cand["t_end"] == 26.0 and any(t["speaker"] == "avery" and t["text"].startswith("Hi Jordan") for t in tt)
    assert len(db.q("SELECT * FROM knockouts WHERE interview_id=?", iid)) == len(ROLE.knockouts)  # unreached knock-outs are stored as unclear
    assert ingest.ingest("26_interview_test_1") == iid  # idempotent


def make_card(score, rec="Submit"):
    d = good_output(overall_score=score, recommendation=rec)
    return scorecard.assemble(d, ROLE, turns(), [], kos(), [], {}, cfg.SAMPLE_LABEL)


def test_api_scorecard_states_dashboard_passcode_ranking_and_recording():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    api.install(app)
    c = TestClient(app)
    ids = {}
    for i, (name, score) in enumerate([("Low Sample", 40), ("High Sample", 90), ("Mid Sample", 65)]):
        stem = f"26_interview_test_r{i}"
        write_session(stem)
        ids[name] = ingest.ingest(stem, candidate_name=name, label=cfg.SAMPLE_LABEL, is_sample=True, sample_kind="strong")
        card = make_card(score)
        with db.tx() as cx:
            cx.execute("INSERT INTO scorecards (interview_id,status,attempts,json,overall_score,recommendation,updated_at) VALUES (?,?,?,?,?,?,0)",
                       (ids[name], "ready", 1, db.dumps(card), card["overall_score"], card["recommendation"]))
            cx.execute("UPDATE interviews SET status='ready' WHERE id=?", (ids[name],))
    live = ingest.create_interview()
    assert c.get(f"/api/interviews/{live}/scorecard").json()["status"] == "pending"
    assert c.get("/api/interviews/deadbeef/scorecard").status_code == 404
    body = c.get(f"/api/interviews/{ids['High Sample']}/scorecard").json()
    assert body["status"] == "ready" and body["label"] == "Sample candidate (simulated)" and body["is_sample"] and body["scorecard"]["label"] == body["label"]
    # dashboard: refused without the cookie, refused with a wrong passcode, ranked after a right one
    assert c.get("/api/dashboard/roles/5905").status_code == 401
    assert c.post("/api/dashboard/login", json={"passcode": "nope"}).status_code == 401
    assert c.post("/api/dashboard/login", json={"passcode": "open-sesame"}).status_code == 200
    rows = c.get("/api/dashboard/roles/5905").json()["candidates"]
    assert [r["candidate_name"] for r in rows[:3]] == ["High Sample", "Mid Sample", "Low Sample"] and [r["rank"] for r in rows[:3]] == [1, 2, 3]
    assert all(r["label"] == "Sample candidate (simulated)" and r["is_sample"] for r in rows) and len(rows[0]["knockouts"]) == 4
    assert c.get("/api/roles/9999").status_code == 404 and c.get("/api/roles").json()[0]["id"] == "5905"
    # recording: served with byte ranges so the page can start playback at a quote's timestamp
    r = c.get(f"/api/interviews/{ids['High Sample']}/recording", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert "noindex" in c.get("/api/roles").headers["x-robots-tag"]


def test_dashboard_is_closed_when_no_passcode_is_configured():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    api.install(app)
    old, cfg.DASHBOARD_PASSCODE = cfg.DASHBOARD_PASSCODE, ""
    try:
        assert TestClient(app).get("/api/dashboard/roles/5905").status_code == 503
    finally:
        cfg.DASHBOARD_PASSCODE = old


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
