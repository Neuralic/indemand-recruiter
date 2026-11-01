"""Backend tests. No network: Vapi and Claude are faked. Run:  .venv-app/Scripts/python.exe -m pytest app/tests -q"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app.config as cfg  # noqa: E402
from app import scorecard, store, vapi  # noqa: E402
from role_config import load_role  # noqa: E402

ROLE = load_role("5905")
TOKEN = "t" * 40
CALL_ID = "019a0000-aaaa-bbbb-cccc-111122223333"

TURNS = [
    {"speaker": "interviewer", "text": "What is prompting you to look at a move right now?", "t_start": 5.0, "t_end": 9.0},
    {"speaker": "candidate", "text": "I have been the lead electrical engineer at a gas plant for eleven years and I want to run a team.", "t_start": 10.0, "t_end": 18.0},
    {"speaker": "interviewer", "text": "Are you legally eligible to work in Canada?", "t_start": 30.0, "t_end": 33.0},
    {"speaker": "candidate", "text": "Yes I am a Canadian citizen.", "t_start": 34.0, "t_end": 36.0},
    {"speaker": "candidate", "text": "I stamped about forty sets of drawings under APEGA last year and I reviewed every single one of them myself before sealing.", "t_start": 80.0, "t_end": 90.0},
]


def good_output() -> dict:
    return {
        "overall_score": 80, "headline": "Experienced plant electrical lead; strong on stamping, light on team size.",
        "competencies": [{"id": c.id, "score": 4, "confidence": "high", "rationale": "Gave a specific example.",
                          "quotes": ["I stamped about forty sets of drawings under APEGA last year"]} for c in ROLE.competencies],
        "knockouts": [{"id": k.id, "status": "unclear", "answer": "Not asked", "quote": ""} for k in ROLE.knockouts],
        "strengths": ["Stamps drawings"], "risks": ["Team size not stated"], "verify_on_call": ["a", "b", "c"],
        "candidate_questions_to_answer": [],
    }


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])


def run(coro):
    return asyncio.run(coro)


# ---- Vapi transcript parsing --------------------------------------------------------------------------------------------------------
def test_vapi_messages_become_turns_and_merge_same_speaker():
    msgs = [{"role": "system", "message": "prompt"}, {"role": "bot", "message": "Hi there.", "secondsFromStart": 1.0},
            {"role": "user", "message": "Hello", "secondsFromStart": 4.0}, {"role": "user", "message": "yes ready", "secondsFromStart": 5.0},
            {"role": "tool_calls", "message": "", "secondsFromStart": 6.0}, {"role": "assistant", "message": "Great.", "secondsFromStart": 9.0}]
    t = vapi.turns_from_messages(msgs)
    assert [x["speaker"] for x in t] == ["interviewer", "candidate", "interviewer"]
    assert t[1]["text"] == "Hello yes ready" and t[0]["t_end"] == 4.0


def test_valid_call_id_rejects_junk():
    assert vapi.valid_call_id(CALL_ID) and not vapi.valid_call_id("../etc/passwd") and not vapi.valid_call_id("")


# ---- scorecard: validator reuse and deterministic rules -------------------------------------------------------------------------------
def test_fabricated_quote_is_dropped_and_real_one_is_timed():
    out = good_output()
    out["competencies"][0]["quotes"] = ["I stamped about forty sets of drawings under APEGA last year", "I once rewired the entire Hoover Dam by myself"]
    card = scorecard.assemble(scorecard.validate_model_output(out, ROLE), ROLE, TURNS, {}, "Live demo session")
    c0 = card["competencies"][0]
    assert [e["quote"] for e in c0["evidence"]] == ["I stamped about forty sets of drawings under APEGA last year"]
    assert c0["evidence"][0]["t"] == 80.0 and card["quote_check"]["dropped"] >= 1


def test_competency_without_verified_quote_is_marked_and_low_confidence():
    out = good_output()
    out["competencies"][1]["quotes"] = ["completely invented sentence about nothing at all"]
    card = scorecard.assemble(scorecard.validate_model_output(out, ROLE), ROLE, TURNS, {}, "x")
    c1 = card["competencies"][1]
    assert c1["evidence"] == [] and c1["confidence"] == "low" and "no verbatim quote" in c1["evidence_note"]


def test_knockout_verdict_needs_a_verified_quote_else_unclear():
    out = good_output()
    ko = {k["id"]: k for k in out["knockouts"]}
    ko["work_auth"].update(status="pass", answer="Citizen", quote="Yes I am a Canadian citizen")
    ko["pe_apega"].update(status="fail", answer="Not registered", quote="I am not registered with anyone ever")  # not in transcript
    card = scorecard.assemble(scorecard.validate_model_output(out, ROLE), ROLE, TURNS, {}, "x")
    by = {k["id"]: k for k in card["knockouts"]}
    assert by["work_auth"]["status"] == "pass" and by["work_auth"]["quote_verified"]
    assert by["pe_apega"]["status"] == "unclear" and not by["pe_apega"]["quote_verified"]


def test_failed_required_knockout_caps_recommendation_at_hold():
    rec, why = scorecard.recommendation_for(90, [{"id": "pe_apega", "required": True, "status": "fail"}])
    assert rec == "Hold" and "pe_apega" in why
    assert scorecard.recommendation_for(90, [])[0] == "Submit" and scorecard.recommendation_for(60, [])[0] == "Hold"
    assert scorecard.recommendation_for(30, [])[0] == "Pass"


def test_validation_rejects_bad_ranges_and_shapes():
    bad = good_output(); bad["overall_score"] = 140
    with pytest.raises(scorecard.ScorecardError):
        scorecard.validate_model_output(bad, ROLE)
    bad = good_output(); bad["competencies"] = bad["competencies"][:-1]
    with pytest.raises(scorecard.ScorecardError):
        scorecard.validate_model_output(bad, ROLE)
    bad = good_output(); bad["verify_on_call"] = ["only one"]
    with pytest.raises(scorecard.ScorecardError):
        scorecard.validate_model_output(bad, ROLE)


def test_protected_words_are_stripped_from_the_scorecard():
    out = good_output()
    out["risks"] = ["Mentioned a pregnancy and being a single mother"]
    card = scorecard.assemble(scorecard.validate_model_output(out, ROLE), ROLE, TURNS, {}, "x")
    assert "pregnan" not in " ".join(card["risks"]).lower()


def test_generate_retries_once_then_succeeds_and_reports_model():
    client = FakeClient([RuntimeError("boom"), good_output()])
    res = run(scorecard.generate(ROLE, TURNS * 2, "assistant-ended-call", "Live demo session", client=client))
    assert res.ok and res.attempts == 2 and res.card["recommendation"] in scorecard.RECOMMENDATIONS
    assert client.calls[0]["model"] == cfg.ANTHROPIC_MODEL and client.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_generate_fails_cleanly_after_two_bad_attempts_and_skips_empty_calls():
    res = run(scorecard.generate(ROLE, TURNS * 2, "x", "x", client=FakeClient([RuntimeError("a"), RuntimeError("b")])))
    assert not res.ok and res.attempts == 2 and res.card is None
    res = run(scorecard.generate(ROLE, [TURNS[0]], "x", "x", client=FakeClient([])))
    assert not res.ok and res.attempts == 0 and "no usable transcript" in res.error


# ---- API: token gate, secrets, caps, labels ----------------------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    store.reset(tmp_path / "t.db")
    monkeypatch.setattr(cfg, "ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(cfg, "VAPI_PUBLIC_KEY", "pub_key_123")
    monkeypatch.setattr(cfg, "VAPI_PRIVATE_KEY", "PRIVATE_SECRET_VALUE")
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "ANTHROPIC_SECRET_VALUE")
    monkeypatch.setattr(cfg, "VAPI_ASSISTANT_ID", "asst_1")
    monkeypatch.setattr(cfg, "KILL_SWITCH", False)
    monkeypatch.setattr(cfg, "MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "DAILY_SESSION_CAP", 20)
    import app.main as main
    main._role_cache.clear()
    with TestClient(main.app) as c:
        yield c
    store.reset()


H = {"x-access-token": TOKEN}


def test_no_token_or_wrong_token_gets_a_plain_404(client):
    assert client.get("/api/config").status_code == 404
    assert client.get("/api/config", headers={"x-access-token": "nope"}).status_code == 404
    assert client.get("/api/dashboard?k=wrong").status_code == 404
    assert client.get("/api/config?k=" + TOKEN).status_code == 200


def test_config_exposes_only_the_public_key(client):
    body = client.get("/api/config", headers=H).text
    assert "pub_key_123" in body and "PRIVATE_SECRET_VALUE" not in body and "ANTHROPIC_SECRET_VALUE" not in body


def test_no_configured_token_fails_closed(client, monkeypatch):
    monkeypatch.setattr(cfg, "ACCESS_TOKEN", "")
    assert client.get("/api/config", headers={"x-access-token": ""}).status_code == 404


def test_noindex_headers_and_robots(client):
    r = client.get("/robots.txt")
    assert "Disallow: /" in r.text and "noindex" in r.headers["x-robots-tag"]
    assert client.get("/healthz").headers["x-robots-tag"].startswith("noindex")


def test_session_caps_and_kill_switch(client, monkeypatch):
    assert client.post("/api/sessions", headers=H).status_code == 200
    r = client.post("/api/sessions", headers=H)
    assert r.status_code == 429 and "in progress" in r.json()["detail"]
    monkeypatch.setattr(cfg, "KILL_SWITCH", True)
    assert client.post("/api/sessions", headers=H).status_code == 503
    monkeypatch.setattr(cfg, "KILL_SWITCH", False)
    monkeypatch.setattr(cfg, "MAX_CONCURRENT", 50)
    monkeypatch.setattr(cfg, "DAILY_SESSION_CAP", 2)
    assert client.post("/api/sessions", headers=H).status_code == 200  # 2nd of 2 today
    assert client.post("/api/sessions", headers=H).status_code == 429 and "daily limit" in client.post("/api/sessions", headers=H).json()["detail"]


def test_ending_a_session_frees_the_slot(client):
    sid = client.post("/api/sessions", headers=H).json()["session_id"]
    assert client.post("/api/sessions", headers=H).status_code == 429
    assert client.post(f"/api/sessions/{sid}/end", headers=H).status_code == 200
    assert client.post("/api/sessions", headers=H).status_code == 200


def test_live_call_flow_produces_a_scorecard_from_the_vapi_transcript(client, monkeypatch):
    import app.main as main

    async def fake_fetch(call_id, wait_s=60.0, poll_s=2.5):
        return vapi.CallData(call_id, "ended", "assistant-ended-call", 1000.0, 1100.0, 100.0, TURNS, True)

    async def fake_generate(role, turns, ended_reason, label, client=None, on_attempt=None):
        return scorecard.Result(True, scorecard.assemble(good_output(), role, turns, {"scorecard_model": "fake"}, label), "fake", 1, "", 1.0)

    monkeypatch.setattr(vapi, "fetch_finished_call", fake_fetch)
    monkeypatch.setattr(scorecard, "generate", fake_generate)
    sid = client.post("/api/sessions", headers=H).json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/call", json={"call_id": CALL_ID}, headers=H).json()["interview_id"] == CALL_ID
    assert client.get(f"/api/interviews/{CALL_ID}/scorecard", headers=H).json()["status"] == "waiting"
    client.post(f"/api/sessions/{sid}/end", headers=H)
    for _ in range(50):
        d = client.get(f"/api/interviews/{CALL_ID}/scorecard", headers=H).json()
        if d["status"] == "ready":
            break
        time.sleep(0.05)
    assert d["status"] == "ready" and d["label"] == "Live demo session" and d["is_sample"] is False
    assert d["scorecard"]["competencies"][0]["evidence"][0]["t"] == 80.0
    tr = client.get(f"/api/interviews/{CALL_ID}/transcript", headers=H).json()
    assert tr[1]["speaker"] == "candidate"
    dash = client.get("/api/dashboard", headers=H).json()
    assert any(c["id"] == CALL_ID and c["label"] == "Live demo session" and c["rank"] for c in dash["candidates"])


def test_bad_call_id_is_rejected_and_unknown_interview_is_404(client):
    sid = client.post("/api/sessions", headers=H).json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/call", json={"call_id": "../x"}, headers=H).status_code == 400
    monkey_ok = client.get("/api/interviews/not-a-call-id/scorecard", headers=H)
    assert monkey_ok.status_code == 404


def test_samples_load_only_when_labelled_simulated(tmp_path, monkeypatch):
    store.reset(tmp_path / "s.db")
    seed = tmp_path / "samples.json"
    card = scorecard.assemble(good_output(), ROLE, TURNS, {}, cfg.SAMPLE_LABEL)
    good = {"id": "sample-strong", "candidate_name": "Alex Morgan", "label": cfg.SAMPLE_LABEL, "sample_kind": "strong", "turns": TURNS, "card": card}
    monkeypatch.setattr(cfg, "SEED_FILE", seed)
    seed.write_text(json.dumps({"samples": [good]}), encoding="utf-8")
    assert store.seed_samples() == 1
    row = store.get("sample-strong")
    assert row["label"] == cfg.SAMPLE_LABEL and row["kind"] == "sample"
    seed.write_text(json.dumps({"samples": [{**good, "label": "Real candidate"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        store.seed_samples()
    store.reset()


# ---- sample simulation loop (fake model) ------------------------------------------------------------------------------------------
def test_simulation_builds_vapi_shaped_turns_and_stops_on_end_marker():
    from app import seed_samples

    class Fake:
        def __init__(self):
            self.n = 0
            self.messages = SimpleNamespace(create=self._create)

        async def _create(self, **kw):
            self.n += 1
            sysmsg = kw["system"]
            text = ("Thanks for that. Goodbye! " + seed_samples.END_MARK) if (seed_samples.SIM_NOTE in sysmsg and self.n > 6) else "Some answer here."
            assert kw["messages"][0]["role"] == "user" and kw["messages"][-1]["role"] == "user"
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])

    persona = seed_samples.PERSONAS[0]
    turns = run(seed_samples.simulate(Fake(), "m", "You are Avery. Hi {{ name }}.", "Hi {{name}}, I'm Avery.", persona))
    assert turns[0]["speaker"] == "interviewer" and turns[0]["text"].startswith("Hi Alex")
    assert seed_samples.END_MARK not in " ".join(t["text"] for t in turns) and turns[-1]["speaker"] == "interviewer"
    assert all(a["t_start"] < b["t_start"] for a, b in zip(turns, turns[1:]))


# ---- real Vapi message shapes (captured from a real call, text shortened) --------------------------------------------------------
def test_real_vapi_timing_uses_endtime_and_duration_not_secondsfromstart():
    started = 1793543251.078  # startedAt of the real call, epoch s
    ms = lambda t: int((started + t) * 1000)
    msgs = [
        {"role": "system", "message": "prompt", "time": ms(0), "secondsFromStart": 0},
        # the greeting really spanned 3.7-28.0 s of the recording; Vapi logged it with secondsFromStart 38.1 (a log time, ~10 s late)
        {"role": "bot", "message": "Hi, I'm Avery.", "time": ms(38.1), "endTime": ms(28.046), "duration": 24333, "secondsFromStart": 38.098},
        {"role": "user", "message": "Yes, let's go ahead.", "time": ms(41.6), "endTime": ms(31.642), "duration": 3002, "secondsFromStart": 41.599, "confidence": 0.9},
        {"role": "tool_calls", "message": "", "toolCalls": [{"function": {"name": "endCall"}}], "secondsFromStart": 464.7},
        {"role": "tool_call_result", "name": "endCall", "result": "Success.", "secondsFromStart": 471.8},
    ]
    t = vapi.turns_from_messages(msgs, 470.0, started)
    assert [x["speaker"] for x in t] == ["interviewer", "candidate"]
    assert abs(t[0]["t_start"] - 3.71) < 0.05 and abs(t[0]["t_end"] - 28.05) < 0.05
    assert abs(t[1]["t_start"] - 28.64) < 0.05 and abs(t[1]["t_end"] - 31.64) < 0.05
    # without endTime/duration (older payloads) it falls back to secondsFromStart rather than failing
    t2 = vapi.turns_from_messages([{"role": "user", "message": "hello there", "secondsFromStart": 12.0}], None, started)
    assert t2[0]["t_start"] == 12.0


def test_pick_recording_url_prefers_stereo_then_mono_and_reads_the_real_field_names():
    art = {"recordingUrl": "https://x/mono", "stereoRecordingUrl": "https://x/stereo",
           "recording": {"stereoUrl": "https://x/s2", "mono": {"combinedUrl": "https://x/m2"}},
           "presignedMonoUrl": "https://x/pm?sig=1", "presignedStereoUrl": "https://x/ps?sig=1"}
    assert vapi.pick_recording_url(art) == "https://x/ps?sig=1"
    del art["presignedStereoUrl"], art["presignedMonoUrl"]
    assert vapi.pick_recording_url(art) == "https://x/s2"
    assert vapi.pick_recording_url({}) is None


def test_unusable_call_with_no_customer_audio_says_so(client):
    store.create_live(CALL_ID)
    store.update(CALL_ID, status="unusable", ended_reason="call.in-progress.error-assistant-did-not-receive-customer-audio")
    d = client.get(f"/api/interviews/{CALL_ID}/scorecard", headers=H).json()
    assert d["status"] == "unusable" and "VPN" in d["message"] and "microphone" in d["message"]
    store.update(CALL_ID, ended_reason="customer-ended-call")
    assert "enough of a conversation" in client.get(f"/api/interviews/{CALL_ID}/scorecard", headers=H).json()["message"]


def test_clean_avery_strips_tool_call_text_and_marker():
    from app import seed_samples as ss
    assert ss.clean_avery("endCall\n```") == ("", False)
    assert ss.clean_avery('{"name":"endCall","arguments":{}}\n```') == ("", False)
    assert ss.clean_avery('{"tool":"endCall"} ```') == ("", False)
    assert ss.clean_avery("Thank you, Alex. Take care. " + ss.END_MARK) == ("Thank you, Alex. Take care.", True)
    assert ss.clean_avery("Do you have any questions?") == ("Do you have any questions?", False)


def test_dashboard_names_live_session_and_caps_unscored_rows(client):
    for i in range(5):
        cid = f"0000000{i}-aaaa-bbbb-cccc-111122223333"
        store.create_live(cid)
        store.update(cid, status="unusable", created_at=1000.0 + i)
    store.create_live(CALL_ID)
    store.update(CALL_ID, status="ready", card_json=json.dumps(scorecard.assemble(good_output(), ROLE, TURNS, {}, "Live demo session")), score=80, recommendation="Submit")
    rows = client.get("/api/dashboard", headers=H).json()["candidates"]
    live = [r for r in rows if not r["is_sample"]]
    assert [r["candidate_name"] for r in live] == ["Your interview"] * 3  # 1 scored + only 2 of the 5 unscored
    scored = [r for r in rows if r["rank"] is not None]
    assert CALL_ID in [r["id"] for r in scored] and [r["rank"] for r in scored] == list(range(1, len(scored) + 1))
    assert all(r["score"] is None for r in rows if r["rank"] is None)
    assert {r["label"] for r in live} == {"Live demo session"}


# ---- write-once scorecards: one generation per call, never redone by reloads, repeats or retries -----------------------------------
def _fake_pipeline(monkeypatch, scores):
    """Every generate() call returns the next score (74, 75, ...) so any regeneration is visible as a changed score."""
    calls = []

    async def fake_fetch(call_id, wait_s=60.0, poll_s=2.5):
        return vapi.CallData(call_id, "ended", "assistant-ended-call", 1000.0, 1100.0, 100.0, TURNS, True)

    async def fake_generate(role, turns, ended_reason, label, client=None, on_attempt=None):
        calls.append(1)
        out = good_output()
        out["overall_score"] = scores[min(len(calls) - 1, len(scores) - 1)]
        return scorecard.Result(True, scorecard.assemble(out, role, turns, {"scorecard_model": "fake"}, label), "fake", 1, "", 0.1)

    monkeypatch.setattr(vapi, "fetch_finished_call", fake_fetch)
    monkeypatch.setattr(scorecard, "generate", fake_generate)
    return calls


def _wait_ready(client, cid):
    for _ in range(60):
        d = client.get(f"/api/interviews/{cid}/scorecard", headers=H).json()
        if d["status"] == "ready":
            return d
        time.sleep(0.05)
    raise AssertionError("never became ready")


def test_scorecard_is_written_once_and_survives_reloads_repeat_ends_and_retries(client, monkeypatch):
    calls = _fake_pipeline(monkeypatch, [74, 75, 76])
    sid = client.post("/api/sessions", headers=H).json()["session_id"]
    client.post(f"/api/sessions/{sid}/call", json={"call_id": CALL_ID}, headers=H)
    client.post(f"/api/sessions/{sid}/end", headers=H)
    first = _wait_ready(client, CALL_ID)
    assert first["scorecard"]["overall_score"] == 74 and len(calls) == 1
    for _ in range(3):  # reloads
        assert client.get(f"/api/interviews/{CALL_ID}/scorecard", headers=H).json()["scorecard"]["overall_score"] == 74
    client.post(f"/api/sessions/{sid}/end", headers=H)  # the browser (or a double event) reports the end again
    client.post(f"/api/sessions/{sid}/end", headers=H)
    assert client.post(f"/api/interviews/{CALL_ID}/scorecard/retry", headers=H).status_code == 409  # retry only for failed/unusable
    time.sleep(0.4)  # give any wrongly-started background job time to finish
    after = client.get(f"/api/interviews/{CALL_ID}/scorecard", headers=H).json()
    assert after["status"] == "ready" and after["scorecard"]["overall_score"] == 74 and after["scorecard"]["generated_at"] == first["scorecard"]["generated_at"]
    assert len(calls) == 1, "the model was called again for a call that already had a scorecard"


def test_save_card_never_overwrites_an_existing_scorecard(client):
    store.create_live(CALL_ID)
    a = scorecard.assemble({**good_output(), "overall_score": 74}, ROLE, TURNS, {}, "x")
    b = scorecard.assemble({**good_output(), "overall_score": 75}, ROLE, TURNS, {}, "x")
    assert store.save_card(CALL_ID, a, 1, 1.0) is True
    assert store.save_card(CALL_ID, b, 1, 1.0) is False
    assert store.get(CALL_ID)["score"] == 74


def test_a_call_the_server_has_no_record_of_is_rebuilt_once_and_says_so(client, monkeypatch):
    calls = _fake_pipeline(monkeypatch, [74, 75])
    d = client.get(f"/api/interviews/{CALL_ID}/scorecard", headers=H)  # e.g. the host restarted and lost its disk
    assert d.status_code == 200
    ready = _wait_ready(client, CALL_ID)
    assert ready["rebuilt"] is True and ready["scorecard"]["overall_score"] == 74
    for _ in range(3):
        assert client.get(f"/api/interviews/{CALL_ID}/scorecard", headers=H).json()["scorecard"]["overall_score"] == 74
    assert len(calls) == 1


def test_database_made_before_the_rebuilt_column_is_migrated_in_place(tmp_path):
    import sqlite3
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.executescript("CREATE TABLE interviews (id TEXT PRIMARY KEY, kind TEXT NOT NULL, role_id TEXT NOT NULL, candidate_name TEXT NOT NULL, "
                    "label TEXT NOT NULL, sample_kind TEXT, status TEXT NOT NULL, created_at REAL NOT NULL, ended_at REAL, duration_s REAL, "
                    "ended_reason TEXT, has_recording INTEGER NOT NULL DEFAULT 0, turns_json TEXT, card_json TEXT, score INTEGER, "
                    "recommendation TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, generated_in_s REAL, updated_at REAL);"
                    "INSERT INTO interviews (id, kind, role_id, candidate_name, label, status, created_at) VALUES ('keep', 'live', '5905', 'x', 'Live demo session', 'ready', 1);")
    c.commit(); c.close()
    store.reset(old)
    assert store.get("keep")["rebuilt"] == 0 and store.get("keep")["status"] == "ready"
    store.reset()


def test_healthz_answers_get_and_head_without_a_token(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.head("/healthz").status_code == 200  # uptime monitors default to HEAD
