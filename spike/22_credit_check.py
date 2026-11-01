import os, httpx
from dotenv import load_dotenv
load_dotenv("../.env", override=True)
r = httpx.post("https://api.openai.com/v1/chat/completions", headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
               json={"model": "gpt-4.1-mini", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
print(r.status_code, (r.json().get("error") or {}).get("message", "ok")[:160])
