import os, sys, httpx
from dotenv import load_dotenv
load_dotenv("../.env", override=True)
k = os.environ["OPENAI_API_KEY"]
r = httpx.post("https://api.openai.com/v1/audio/transcriptions", headers={"Authorization": f"Bearer {k}"},
               files={"file": ("m.wav", open(sys.argv[1], "rb"), "audio/wav")},
               data={"model": "whisper-1", "response_format": "verbose_json", "timestamp_granularities[]": "segment"}, timeout=180)
print(r.status_code)
if r.status_code == 200:
    for s in r.json()["segments"]:
        print(f'{s["start"]:6.1f}-{s["end"]:6.1f} nsp={s["no_speech_prob"]:.2f} {s["text"].strip()[:140]}')
else:
    print(r.json().get("error", {}).get("message", "")[:200])
