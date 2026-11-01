"""Phase 0 / item 1+7: what does this OpenAI key actually have access to?

Prints model ids only. Never prints the key.
"""

import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv(override=True)
key = os.environ.get("OPENAI_API_KEY")
assert key, "OPENAI_API_KEY missing from .env"

r = httpx.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
print("GET /v1/models ->", r.status_code)
r.raise_for_status()
ids = sorted(m["id"] for m in r.json()["data"])
print("total models visible:", len(ids))

groups = {
    "realtime": r"realtime",
    "audio/tts/transcribe": r"(tts|transcribe|whisper|audio)",
    "gpt-5*/o-series (reasoning candidates)": r"^(gpt-5|o\d)",
    "gpt-4.1/4o": r"^gpt-4",
}
for name, pat in groups.items():
    hits = [i for i in ids if re.search(pat, i)]
    print(f"\n[{name}] ({len(hits)})")
    for h in hits:
        print("  ", h)
