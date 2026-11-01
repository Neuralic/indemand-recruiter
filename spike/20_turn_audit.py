"""Audit OpenAI turn windows against Silero speech on the recorded mic wav. usage: 20_turn_audit.py <session-stem>"""
import json, sys, wave
import numpy as np, soxr
from loguru import logger
logger.remove()
from pipecat.audio.vad.silero import SileroVADAnalyzer

stem = sys.argv[1]
d = json.load(open(f"out/{stem}.json"))
w = wave.open(f"out/{stem}_mic.wav"); pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16); sr = w.getframerate()
x = soxr.resample(pcm, sr, 16000).astype(np.int16)
v = SileroVADAnalyzer(); v.set_sample_rate(16000)
probs = [float(np.squeeze(v.voice_confidence(x[i:i + 512].tobytes()))) for i in range(0, len(x) - 512, 512)]
step = 512 / 16000
t0 = d["mic_frames"][0][0]
ev = [(e["kind"], e["t"] - t0) for e in d["events"] if e["tap"] == "out" and e["kind"].startswith("User")]
bots = [e["t"] - t0 for e in d["events"] if e["tap"] == "out" and e["kind"] == "BotStartedSpeakingFrame"]
st = None; n = 0
for k, t in ev:
    if k == "UserStartedSpeakingFrame": st = t
    elif st is not None:
        n += 1; a, b = max(0, st - 0.3), t
        p = probs[int(a / step):int(b / step)]
        rms = float(np.sqrt(np.mean(x[int(a * 16000):int(b * 16000)].astype(float) ** 2)))
        reply = next((round(bt - t, 1) for bt in bots if bt >= t), None)
        print(f"turn{n}: {st:6.1f}-{t:6.1f}s dur={t-st:5.1f} silero_speech={sum(q>0.5 for q in p)*step:5.1f}s maxP={max(p, default=0):.2f} rms={rms:6.0f} bot_reply_after={reply}s")
        st = None
print("mic s:", round(len(x) / 16000, 1), "| bot starts:", [round(b, 1) for b in bots])
