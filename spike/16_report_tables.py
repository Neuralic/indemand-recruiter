"""Phase 0b: build the report tables from the saved JSON results (no API calls). Writes out/16_report_tables.md."""

import glob
import json
import statistics

import bakeoff_common as b
import harness as h


def load(pattern: str) -> list[dict]:
    out = []
    for p in sorted(glob.glob(str(h.OUT / pattern))):
        d = json.load(open(p, encoding="utf-8"))
        d["_file"] = p
        out.append(d)
    return out


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


md: list[str] = []

# ---- 1. TTS time-to-first-audio ----
md += ["## TTS time-to-first-audio (Pipecat service, warm websocket, 10 script lines)", "",
       "| arm | provider / model | voice | n | p50 s | p90 s | min | max | first line (cold-ish) s |",
       "|---|---|---|---|---|---|---|---|---|"]
for d in sorted(load("09_ttfa_*.json"), key=lambda d: (d["arm"], d["voice"])):
    md.append(f"| {d['arm']} | {d['provider']} / {d.get('tts_model', '')} | {d['voice']} | {d['n']} | {d['ttfa_p50']} | "
              f"{d['ttfa_p90']} | {d['ttfa_min']} | {d['ttfa_max']} | {d['first_line_s']} |")

# ---- 2. end to end ----
md += ["", "## End-to-end: candidate stops speaking -> first bot audio heard (10 valid turns per session)", "",
       "| arm | config | voice | n valid | p50 s | p90 s | min | max | greeting s | stalls |",
       "|---|---|---|---|---|---|---|---|---|---|"]
e2e = load("13_e2e_*.json")
for d in sorted(e2e, key=lambda d: (d["arm"], d.get("d_provider") or "", d.get("turn_detection", "-"), d["voice"])):
    n = d.get("n_valid", d.get("n"))
    cfg = d.get("turn_detection", "-") + (f" + {d['d_provider']}" if d.get("d_provider") else "")         + (" STT=gpt-realtime-whisper (diagnostic)" if "whisperstt" in d["_file"] else "")
    if d["arm"] in ("B", "C"):
        cfg += f" / {d.get('text_llm')}"
    md.append(f"| {d['arm']} | {cfg} | {d['voice']} | {n} | {d.get("e2e_p50")} | {d.get("e2e_p90")} | {d.get("e2e_min")} | "
              f"{d.get("e2e_max")} | {d.get("greeting_first_audio_s")} | {d.get('stalls', '-')} |")

md += ["", "### Pooled per arm (valid turns only)", "", "| arm | sessions | n | p50 s | p90 s |", "|---|---|---|---|---|"]
groups: dict[str, list[float]] = {}
for d in e2e:
    key = d["arm"] + (f"-{d.get('turn_detection', '-')}" if d["arm"] in ("A", "D") else "") + \
          (f"-{d['d_provider']}" if d.get("d_provider") else "") + ("-whisperSTT" if "whisperstt" in d["_file"] else "")
    vals = [r["e2e_s"] for r in d["rows"] if r.get("valid", r["e2e_s"] is not None)]
    groups.setdefault(key, []).extend(vals)
for k, v in sorted(groups.items()):
    md.append(f"| {k} | {sum(1 for d in e2e if d['arm'] == k[0])} | {len(v)} | {b.pct(v, 50)} | {b.pct(v, 90)} |")

md += ["", "### Where the time goes (median seconds after the candidate stops, valid turns)", "",
       "| session | VAD stop | final transcript | turn declared over | first LLM token | first TTS audio | heard |",
       "|---|---|---|---|---|---|---|"]
for d in sorted(e2e, key=lambda d: (d["arm"], d.get("d_provider") or "", d.get("turn_detection", "-"), d["voice"])):
    rows = [r for r in d["rows"] if r.get("valid", True) and r["e2e_s"]]
    c = lambda k: med([r["components_s"].get(k) for r in rows])  # noqa: E731
    name = f"{d['arm']} {d['voice']} {d.get('turn_detection', '-')}" + (" whisperSTT" if "whisperstt" in d["_file"] else "") + (f" +{d['d_provider']}" if d.get("d_provider") else "")
    md.append(f"| {name} | {c('VADUserStoppedSpeakingFrame')} | {c('TranscriptionFrame')} | {c('UserStoppedSpeakingFrame')} | "
              f"{c('LLMTextFrame')} | {c('TTSAudioRawFrame')} | {med([r['e2e_s'] for r in rows])} |")

# ---- 3. server turn detection probe ----
md += ["", "## OpenAI server turn detection alone (raw websocket, no Pipecat): speech end -> speech_stopped event", "",
       "| mode | n | p50 s | p90 s | min | max |", "|---|---|---|---|---|---|"]
for d in load("14_vad_probe_*.json"):
    md.append(f"| {d['mode']} | {d['n']} | {d['p50']} | {d['p90']} | {d['min']} | {d['max']} |")

# ---- 4. soak ----
md += ["", "## 10-minute websocket soak", "",
       "| provider | session s | lines with audio | idle gap s | ttfa after gap s | events | reconnect log lines |",
       "|---|---|---|---|---|---|---|"]
for d in load("15_soak_*.json"):
    gap_row = next((r for r in d["rows"] if (r.get("idle_before_s") or 0) > 300), {})
    md.append(f"| {d['provider']} ({d.get('tts_model')}) | {d['session_s']} | {d['lines_ok']}/{d['lines_total']} | "
              f"{gap_row.get('idle_before_s')} | {gap_row.get('ttfa_s')} | "
              f"{[(e['t'], e['event']) for e in d['events']]} | {d['reconnect_log_lines']} |")

open(h.OUT / "16_report_tables.md", "w", encoding="utf-8").write("\n".join(md))
print("\n".join(md))
