import importlib, importlib.metadata as m, sys
want = dict(l.strip().split("==") for l in open("constraints-win312.txt") if "==" in l)
norm = lambda s: s.lower().replace("_", "-").replace(".", "-")
have = {norm(d.metadata["Name"]): d.version for d in m.distributions()}
bad = [(k, v, have.get(norm(k))) for k, v in want.items() if have.get(norm(k)) != v]
extra = sorted(set(have) - {norm(k) for k in want} - {"pip", "setuptools"})
print("python", sys.version.split()[0], "| pins", len(want), "| mismatches", bad, "| extras", extra)
print("litellm/brotli installed:", [k for k in have if k in ("litellm", "brotli", "brotlicffi")])
for mod in ("pipecat", "onnxruntime", "aiortc", "av", "aioice", "numpy", "soxr", "websockets", "httpx", "numba", "pipecat.audio.vad.silero",
            "pipecat.services.openai.realtime.llm", "pipecat.services.cartesia.tts", "pipecat.transports.smallwebrtc.transport", "pipecat.runner.run"):
    try:
        importlib.import_module(mod); print("ok  ", mod)
    except Exception as e:
        print("FAIL", mod, repr(e)[:200])
import onnxruntime, av
print("onnxruntime", onnxruntime.__version__, "| av", av.__version__)
from pipecat.audio.vad.silero import SileroVADAnalyzer
v = SileroVADAnalyzer(); v.set_sample_rate(16000); print("silero model loaded, conf on silence:", float(v.voice_confidence(b"\x00" * 1024)))
