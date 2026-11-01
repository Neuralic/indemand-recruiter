"""Live-mic latency: true end of user speech (Silero on recorded mic wav) -> first bot audio write (BotStartedSpeakingFrame).
usage: 17b_analyze_live.py <session-stem>"""
import json, sys, wave
import numpy as np, soxr
from loguru import logger
logger.remove()
from pipecat.audio.vad.silero import SileroVADAnalyzer
import bakeoff_common as b

stem = sys.argv[1]
d = json.load(open(f"out/{stem}.json"))
w = wave.open(f"out/{stem}_mic.wav"); pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
x = soxr.resample(pcm, w.getframerate(), 16000).astype(np.int16)
v = SileroVADAnalyzer(); v.set_sample_rate(16000)
step = 512 / 16000
probs = [float(np.squeeze(v.voice_confidence(x[i:i + 512].tobytes()))) for i in range(0, len(x) - 512, 512)]
t0 = d["mic_frames"][0][0]
ev = d["events"]
rel = lambda e: e["t"] - t0
bots = [rel(e) for e in ev if e["tap"] == "out" and e["kind"] == "BotStartedSpeakingFrame"]
bstop = [rel(e) for e in ev if e["tap"] == "out" and e["kind"] == "BotStoppedSpeakingFrame"]
def comp(kind, tap, after):
    return next((rel(e) for e in ev if e["tap"] == tap and e["kind"] == kind and rel(e) >= after), None)
rows = []
for k, bt in enumerate(bots[1:], start=1):  # bots[0] = greeting
    prev_stop = next((s for s in reversed(bstop) if s < bt), 0)
    sp = [i for i, q in enumerate(probs) if q > 0.5 and prev_stop <= (i + 1) * step <= bt]
    if not sp: continue
    end = (sp[-1] + 1) * step  # last speech chunk end
    if bt - end > 15: continue
    r = {"reply": k, "user_end": round(end, 2), "e2e": round(bt - end, 2)}
    for name, kind, tap in (("vad_stop", "UserStoppedSpeakingFrame", "out"), ("first_llm", "LLMTextFrame", "llm"), ("first_tts", "TTSAudioRawFrame", "tts")):
        t = comp(kind, tap, end - 0.3)
        r[name] = round(t - end, 2) if t else None
    rows.append(r); print(r)
vals = [r["e2e"] for r in rows if r["e2e"] > 0]
print(f"bot replies started: {len(bots)} (incl greeting); stopped: {len(bstop)}; measured turns n={len(vals)}")
print("e2e server-side p50", b.pct(vals, 50), "p90", b.pct(vals, 90), "min", min(vals, default=None), "max", max(vals, default=None))
print("user turns kept/dropped:", sum(e["kind"] == "kept" for e in ev if e["tap"] == "user_text"), sum(e["kind"] == "dropped" for e in ev if e["tap"] == "user_text"))
print("max_loop_lag_s", d["max_loop_lag_s"])
