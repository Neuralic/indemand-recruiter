"""Phase 0b: pick the text LLM for the cascaded arms by time-to-first-token on a realistic interview turn (n=6 each)."""
import asyncio, json, sys, time
from openai import AsyncOpenAI
import bakeoff_common as b
import harness as h
import interview_spec as spec

PERSONA = open("bakeoff_persona.txt", encoding="utf-8").read()
MSGS = [{"role": "system", "content": PERSONA},
        {"role": "assistant", "content": "Thanks, Dana. What's got you open to a move right now?"},
        {"role": "user", "content": "Honestly the plant I'm at is being sold next year and I want to stay in heavy industry."}]
MODELS = sys.argv[1:] or ["gpt-4.1", "gpt-4.1-mini", "gpt-5.1-chat-latest", "gpt-5.2-chat-latest", "gpt-5-chat-latest"]

async def one(client, model):
    t0 = time.perf_counter(); first = None; text = ""
    stream = await client.chat.completions.create(model=model, messages=MSGS, stream=True, max_tokens=120)
    async for ch in stream:
        if ch.choices and ch.choices[0].delta.content:
            first = first or (time.perf_counter() - t0); text += ch.choices[0].delta.content
    return first, time.perf_counter() - t0, text

async def main():
    client = AsyncOpenAI(api_key=h.api_key()); out = {}
    for m in MODELS:
        ts = []
        try:
            for _ in range(6):
                f, tot, text = await one(client, m); ts.append(f); await asyncio.sleep(0.5)
            out[m] = {"ttft_p50": b.pct(ts, 50), "ttft_p90": b.pct(ts, 90), "sample": text[:160]}
        except Exception as e:
            out[m] = {"error": str(e)[:160]}
        print(m, out[m])
    json.dump(out, open(h.OUT / "12_llm_ttft.json", "w"), indent=1)
asyncio.run(main())
