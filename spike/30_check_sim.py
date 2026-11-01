"""Pass/fail checks for one simulated interview (stub voice) against its server log.

usage: 30_check_sim.py <strong|vague|offtopic> [session_stem]     (default: the newest out/26_interview_*.json)
Exit code 1 if any check fails. Every check reads the session json (events + interview summary); nothing is judged by ear.
"""

import json
import re
import sys
import wave

import harness as h

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
from interview import CLOSE, LINE_OFFTOPIC_END  # noqa: E402
from role_config import load_role  # noqa: E402

ROLE = load_role("5905")


def load(stem):
    if stem:
        p = h.OUT / f"{stem}.json"
    else:
        p = sorted(h.OUT.glob("26_interview_*.json"), key=lambda q: q.stat().st_mtime)[-1]
    return p, json.loads(p.read_text(encoding="utf-8"))


def wav_seconds(path):
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:  # noqa: BLE001
        return None


def main():
    scen = sys.argv[1]
    p, d = load(sys.argv[2] if len(sys.argv) > 2 else "")
    ev, iv = d["events"], d["interview"]
    actions = [e for e in ev if e["tap"] == "director" and e["kind"] == "action"]
    texts = [e for e in ev if e["tap"] == "user_text"]
    turns = [e for e in ev if e["tap"] == "director" and e["kind"] == "turn"]
    said = [a.get("text") or "" for a in actions]
    res = []

    def check(name, ok, detail=""):
        res.append((name, bool(ok), detail))

    check("tts mode is the stub (no credits spent)", "STUB" in d["config"]["tts"], d["config"]["tts"][:60])
    check("no 'didn't quite catch that' was ever spoken", not any("didn't quite catch" in t for t in said)
          and not any(a["action_kind"] == "reask" for a in actions), "reask actions=%d" % sum(a["action_kind"] == "reask" for a in actions))
    probes = [a for a in actions if a["action_kind"] == "probe"]
    per = {}
    for a in probes:
        per[a["step"]] = per.get(a["step"], 0) + 1
    check("every competency probed at most once", all(v <= 1 for v in per.values()) and len(probes) == len(iv["probe_used"]),
          f"probe actions={len(probes)} probe_used={iv['probe_used']}")
    check("protected info count is 0", iv["protected_info_volunteered_count"] == 0)
    # the bot-audio recording exists and covers the call
    stem = p.stem
    mic_s, bot_s = wav_seconds(h.OUT / f"{stem}_mic.wav"), wav_seconds(h.OUT / f"{stem}_bot.wav")
    check("bot audio wav recorded and covers the call", bool(mic_s and bot_s and bot_s >= 0.9 * mic_s) and (h.OUT / f"{stem}_mix.wav").exists(),
          f"mic={mic_s and round(mic_s)}s bot={bot_s and round(bot_s)}s")
    merged = [t for t in turns if t.get("merged_with", 0) >= 1]
    waits = [e for e in ev if e["tap"] == "director" and e["kind"] == "continuation_wait"]
    if scen != "offtopic":
        check("split answers were merged (turn events with merged_with>=1)", len(merged) >= 1, f"merged turns={len(merged)} incomplete={sum(t['incomplete'] for t in turns)} waits={len(waits)}")

    if scen == "strong":
        check("ended completed", iv["ended"] == "completed", iv["ended"])
        check("background fragment 'All right, yeah so' was judged incomplete and held", any(t["incomplete"] for t in turns))
        q_tech = ROLE.competencies[0].primary_question
        check("the first competency question was asked exactly once (fragment + answer = ONE turn)", sum(t == q_tech for t in said) == 1,
              f"count={sum(t == q_tech for t in said)}")
        tech_probe = [a for a in probes if a["step"] == "technical_electrical"]
        check("2 s thinking pause inside the probe answer: ONE probe, then straight on to the next question", len(tech_probe) == 1)
        ev_tech = [e for e in iv["evidence"] if e["competency_id"] == "technical_electrical" and e["step"] == "technical_electrical"]
        check("the probe answer (two halves) was judged together, not forced weak",
              ev_tech and not any(e["assessment_forced_weak"] for e in ev_tech),
              "; ".join(f"{e['assessment']} forced={e['assessment_forced_weak']}" for e in ev_tech))
        perm = [t for t in said if t.startswith("Of course, we'll make time for your questions at the end.")]
        check("'Can I ask a question?' -> one short line + short repeat, once", len(perm) == 1 and len(perm[0].split()) < 30,
              (perm[0][:120] if perm else "none"))
        gl = [t for t in said if t.startswith("That's the professional engineer designation")]
        check("'what is P.Eng?' -> the fixed glossary sentence, once, then a short repeat", len(gl) == 1 and len(gl[0].split()) < 40,
              (gl[0][:140] if gl else "none"))
        haz = [e for e in texts if "national electrical" in (e["raw"] or "").lower()]
        check("hazardous-locations answer reached the evaluator uncut (raw == clean)", haz and all(e["clean"] == e["raw"] for e in haz),
              (haz[0]["clean"][:100] if haz else "no such transcript"))
        kos = iv["knockouts"]
        req = [k.id for k in ROLE.knockouts if k.required]
        check("all four required knock-outs recorded pass with a verified quote",
              all(kos.get(k, {}).get("status") == "pass" and kos[k].get("quote_verified") for k in req),
              {k: (kos.get(k, {}).get("status"), kos.get(k, {}).get("quote_verified")) for k in req})
        client = [t for t in said if "established engineering organization" in t]
        check("'Who is the client?' answered from the posting only", len(client) == 1 and "Stefanie will confirm anything beyond that" in client[0])
        ben = [t for t in said if t.startswith("Stefanie will confirm that with you.") and "Anything else" in t]
        check("salary/benefits deferred to Stefanie with no numbers", len(ben) >= 1 and not any(re.search(r"\d|\$", t) for t in ben))
        check("closing is the fixed line and promises no timeline", said and said[-1] == CLOSE.format(name="David")
              and not re.search(r"\b(day|days|hour|hours|week|weeks|tomorrow|tonight|soon|within)\b", said[-1], re.I), said[-1][:80] if said else "")
    elif scen == "vague":
        check("ended completed", iv["ended"] == "completed", iv["ended"])
        comp_ids = [c.id for c in ROLE.competencies]
        check("each of the 6 competencies probed exactly once", sorted(per) == sorted(comp_ids) and all(v == 1 for v in per.values()), per)
        forced = {e["competency_id"] for e in iv["evidence"] if e["assessment_forced_weak"] and e["competency_id"] == e["step"]}
        check("every still-vague competency logged weak by code", set(comp_ids) <= forced, sorted(set(comp_ids) - forced))
        check("the failed knock-out (pe_apega) is recorded as fail, not hidden", iv["knockouts"].get("pe_apega", {}).get("status") == "fail",
              iv["knockouts"].get("pe_apega", {}).get("status"))
    elif scen == "offtopic":
        check("ended off_topic", iv["ended"] == "off_topic", iv["ended"])
        red = [a for a in actions if a["action_kind"] == "redirect"]
        check("two redirects (each with the SHORT pending question), then the close", len(red) == 2
              and all(len((a.get("text") or "").split()) < 30 for a in red) and said[-1] == LINE_OFFTOPIC_END, [len((a.get("text") or "").split()) for a in red])
        perm = [t for t in said if t.startswith("Of course, we'll make time for your questions at the end.")]
        check("'Can I ask a question?' answered once", len(perm) == 1)
    w = max(len(n) for n, _, _ in res)
    bad = 0
    print(f"=== {p.stem}  scenario={scen}  ended={iv['ended']}  elapsed={iv['elapsed_s']}s  tts={d['config']['tts'][:40]}")
    for n, ok, det in res:
        bad += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {n:<{w}}  {str(det)[:150] if (det != '' and (not ok or True)) else ''}")
    ack_cut = sum(1 for e in ev if e["tap"] == "director" and e["kind"] == "ack_cut")
    dropped = sum(1 for e in ev if e["tap"] == "director" and e["kind"] == "ack_dropped")
    print(f"  info: ack_cut={ack_cut} ack_dropped={dropped} fragment_no_reask={sum(1 for e in ev if e['tap']=='director' and e['kind']=='fragment_no_reask')} "
          f"failover={[e['kind'] for e in ev if e['tap']=='tts_failover']}")
    sys.exit(1 if bad else 0)


main()
