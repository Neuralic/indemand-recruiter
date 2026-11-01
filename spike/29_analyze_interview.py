"""Summarise one 26_live_interview session: timings per turn, what the director did, and the interview record.
usage: 29_analyze_interview.py <session stem, e.g. 26_interview_harlow_600_181520>"""

import json
import statistics as st
import sys

import harness as h

stem = sys.argv[1]
d = json.load(open(h.OUT / f"{stem}.json", encoding="utf-8"))
ev = d["events"]
t0 = ev[0]["t"]


def R(t):
    return round(t - t0, 2)


def pct(xs, p):
    xs = sorted(xs)
    return xs[max(0, min(len(xs) - 1, int(-(-p * len(xs) // 100)) - 1))] if xs else None


stops = [e for e in ev if e["tap"] == "openai_vad" and e["kind"] == "speech_stopped"]
starts = [e for e in ev if e["tap"] == "out" and e["kind"] == "BotStartedSpeakingFrame"]
stopsb = [e for e in ev if e["tap"] == "out" and e["kind"] == "BotStoppedSpeakingFrame"]
verdicts = [e for e in ev if e["tap"] == "director" and e["kind"] == "verdict"]
actions = [e for e in ev if e["tap"] == "director" and e["kind"] == "action"]
acks = [e for e in ev if e["tap"] == "director" and e["kind"] == "ack"]
texts = [e for e in ev if e["tap"] == "user_text"]

print(f"=== {stem}  evaluator={d['config']['evaluator']}  ended={d['interview']['ended']}  elapsed={d['interview']['elapsed_s']}s")
print("tts:", d["config"].get("tts"))
fo = [e for e in ev if e["tap"] == "tts_failover"]
print("-- TTS failover events --" if fo else "-- TTS failover events: none --")
for e in fo:
    print(f"  {R(e['t']):7.1f}s {e['kind']:20s} " + " ".join(f"{k}={v}" for k, v in e.items() if k not in ("tap", "kind", "t")))
for e in [e for e in ev if e["tap"] == "director" and e["kind"] in ("voice_failed", "ending", "finalize")]:
    print(f"  {R(e['t']):7.1f}s director {e['kind']} {e.get('why', e.get('reason', ''))}")
print("\n-- candidate turns (kept/dropped) --")
for e in texts:
    print(f"  {R(e['t']):7.1f}s {e['kind']:7s} {e.get('clean') or e.get('raw')!r:.110}")

print("\n-- timeline: stop -> ack audio -> question audio (server side, seconds after OpenAI speech_stopped) --")
rows = []
for s in stops:
    nxt_start = [b for b in starts if b["t"] > s["t"]]
    nxt_act = [a for a in actions if a["t"] > s["t"] and a["action_kind"] in ("question", "probe", "reask", "redirect", "answer", "close")]
    v = next((x for x in verdicts if x["t"] > s["t"]), None)
    ack = next((x for x in acks if x["t"] > s["t"]), None)
    if not nxt_start:
        continue
    a_audio = nxt_start[0]["t"] - s["t"]
    q_audio = None
    if nxt_act:
        qs = [b for b in starts if b["t"] > nxt_act[0]["t"]]
        q_audio = (qs[0]["t"] - s["t"]) if qs else None
    rows.append((R(s["t"]), a_audio, (v or {}).get("classify_s"), (nxt_act[0]["t"] - s["t"]) if nxt_act else None, q_audio,
                 (v or {}).get("turn_kind"), (v or {}).get("specificity"), (ack or {}).get("text"), nxt_act[0]["action_kind"] if nxt_act else None))
    print(f"  stop@{rows[-1][0]:7.1f}s ack_audio=+{a_audio:4.2f} classify={rows[-1][2]}s action_at=+{rows[-1][3] and round(rows[-1][3], 2)} "
          f"question_audio=+{q_audio and round(q_audio, 2)} kind={rows[-1][5]} spec={rows[-1][6]} ack={rows[-1][7]!r} -> {rows[-1][8]}")
ack_l = [r[1] for r in rows]
q_l = [r[4] for r in rows if r[4]]
cl = [r[2] for r in rows if r[2]]
if ack_l:
    print(f"  stop->ACK audio:      n={len(ack_l)} p50={st.median(ack_l):.2f} p90={pct(ack_l, 90):.2f}")
if q_l:
    print(f"  stop->QUESTION audio: n={len(q_l)} p50={st.median(q_l):.2f} p90={pct(q_l, 90):.2f}")
if cl:
    print(f"  classify latency:     n={len(cl)} p50={st.median(cl):.2f} p90={pct(cl, 90):.2f} max={max(cl):.2f}")
print("  events:", {k: sum(1 for e in ev if e["tap"] == "director" and e["kind"] == k) for k in
                    sorted({e["kind"] for e in ev if e["tap"] == "director"})})

print("\n-- what the bot said (director actions) --")
for a in actions:
    print(f"  {R(a['t']):7.1f}s [{a['stage']}/{a['step']}] {a['action_kind']:8s} {a['text'][:150]}")
print("\n-- model acknowledgements (raw -> spoken) --")
for a in acks:
    print(f"  {R(a['t']):7.1f}s {a['reason']:8s} raw={a['raw'][:80]!r} spoken={a['text']!r}")

iv = d["interview"]
print("\n-- stage log --")
for s in iv["stage_log"]:
    print(f"  {s['t']:7.1f}s {s['stage']:20s} {s['step']:22s} {s['note']}")
print("\n-- evidence --")
for e in iv["evidence"]:
    print(f"  {e['step']:18s} {e['competency_id']:18s} {e['assessment']:8s} forced_weak={e['assessment_forced_weak']} score={e['match_score']} {e['quote'][:90]!r}")
print("-- rejected quotes --")
for r in iv["rejected_quotes"]:
    print(f"  {r}")
print("-- knockouts --")
for k, v in iv["knockouts"].items():
    print(f"  {k:14s} {v['status']:8s} verified={v['quote_verified']} answer={v['answer'][:50]!r} quote={v['quote'][:60]!r}")
print("-- flags --")
for f in iv["flags"]:
    print(f"  {f['t']:7.1f}s {f['topic']}: {f['reason'][:100]}")
print("probe_used:", iv["probe_used"], "| protected_volunteered_count:", iv["protected_info_volunteered_count"])
