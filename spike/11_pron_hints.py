"""Phase 0b: do the providers' pronunciation controls actually change the audio?

For one voice per provider whose DEFAULT is wrong/odd for a term (found in 10_pronunciation), synthesise two test lines
baseline vs hinted and judge both with ASR + the audio judge from 10_pronunciation. Targets are MY assumed industry
pronunciations (LOTO = LOH-toh, SCADA = SKAY-duh, APEGA = uh-PEG-uh, Maximo = MAK-sih-moh, P.Eng = pee-ENG); David must
confirm them by ear - the test proves the MECHANISM works, not that the targets are canonical.

  Cartesia: pronunciation dictionary via API (sounds-like items, IPA items) passed as pronunciation_dict_id; inline <<IPA>>.
  Fish:     inline <|phoneme_start|>ARPABET<|phoneme_end|> tags (no dictionary feature exists); applied through Pipecat's
            text_transforms hook, which would be the production wiring.
  Realtime: a pronunciation instruction in the system prompt (no dictionary exists).
Outputs: out/bakeoff/hints/*.wav, out/11_pron_hints.json
"""

import asyncio
import importlib.util
import json
import os
import re
import sys

import httpx
import websockets

import bakeoff_common as b
import harness as h
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.services.cartesia.tts import format_cartesia_pronunciation
from pipecat.workers.runner import WorkerRunner

spec = importlib.util.spec_from_file_location("t09", "09_tts_ttfa.py")
t09 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t09)
spec2 = importlib.util.spec_from_file_location("t10", "10_pronunciation.py")
t10 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(t10)

HINT_DIR = b.BAKE / "hints"
HINT_DIR.mkdir(exist_ok=True)
T1 = "How do you run your LOTO program, and how do you handle SCADA alarms?"
T2 = "Do you hold P.Eng registration with APEGA, and have you used Maximo?"
TARGET_TERMS = ["LOTO", "SCADA", "P.Eng", "APEGA", "Maximo"]
TARGET = {"LOTO": "LOH-toh", "SCADA": "SKAY-duh", "APEGA": "uh-PEG-uh", "Maximo": "MAK-sih-moh", "P.Eng": "pee-ENG"}
IPA = {"LOTO": "ˈloʊtoʊ", "SCADA": "ˈskeɪdə", "APEGA": "əˈpɛɡə", "Maximo": "ˈmæksɪmoʊ", "P.Eng": "ˈpiːˈɛŋ"}
ARPA = {"LOTO": "L OW1 T OW0", "SCADA": "S K EY1 D AH0", "APEGA": "AH0 P EH1 G AH0", "Maximo": "M AE1 K S IH0 M OW2",
        "P.Eng": "P IY1 EH1 NG"}
CARTESIA_H = {"Authorization": f"Bearer {os.environ['CARTESIA_API_KEY']}", "Cartesia-Version": "2026-08-14"}


def create_dict(name: str, items: list[dict]) -> str:
    r = httpx.post("https://api.cartesia.ai/pronunciation-dicts/", headers=CARTESIA_H, timeout=30,
                   json={"name": name, "description": "Phase 0b spike, safe to delete", "items": items})
    print("create dict", name, r.status_code, r.text[:200] if r.status_code != 200 else "")
    r.raise_for_status()
    return r.json()["id"]


def delete_dict(pid: str) -> int:
    return httpx.delete(f"https://api.cartesia.ai/pronunciation-dicts/{pid}", headers=CARTESIA_H, timeout=30).status_code


async def speak(provider: str, voice_id: str, texts: list[str], extra=None, transform=None) -> list[bytes]:
    tts = b.make_tts(provider, voice_id, **(extra or {}))
    if transform:
        tts.add_text_transformer(transform)
    sink = t09.Sink()
    worker = PipelineWorker(Pipeline([t09.Stamp(sink), tts, sink]),
                            params=PipelineParams(audio_out_sample_rate=24000, enable_metrics=True))
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(3.0)
    out = []
    for text in texts:
        sink.reset()
        await worker.queue_frames([TTSSpeakFrame(text)])
        try:
            await asyncio.wait_for(sink.stopped.wait(), 45)
        except TimeoutError:
            pass
        await asyncio.sleep(0.3)
        out.append(bytes(sink.pcm))
    await runner.cancel()
    try:
        await asyncio.wait_for(task, 10)
    except Exception:  # noqa: BLE001
        pass
    return out


async def realtime_read(voice: str, texts: list[str], extra_instructions: str = "") -> list[bytes]:
    out = []
    for text in texts:
        async with await websockets.connect(
                "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
                additional_headers={"Authorization": f"Bearer {h.api_key()}"}, max_size=None) as ws:
            import base64
            while json.loads(await ws.recv())["type"] != "session.created":
                pass
            await ws.send(json.dumps({"type": "session.update", "session": {
                "type": "realtime", "model": "gpt-realtime-2.1", "output_modalities": ["audio"],
                "instructions": "Read the user's text aloud EXACTLY as written, in a warm, natural, conversational "
                                "Canadian-professional tone. Say nothing else. " + extra_instructions,
                "audio": {"output": {"voice": voice}}}}))
            while json.loads(await ws.recv())["type"] != "session.updated":
                pass
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
            await ws.send(json.dumps({"type": "response.create"}))
            pcm = bytearray()
            while True:
                evt = json.loads(await asyncio.wait_for(ws.recv(), 60))
                if evt["type"] == "response.output_audio.delta":
                    pcm.extend(base64.b64decode(evt["delta"]))
                elif evt["type"] in ("response.done", "error"):
                    break
        out.append(bytes(pcm))
    return out


ANCHORS = {  # term -> (word before, word after) in the test lines, to find the term's span in whisper's word list
    "LOTO": ("your", "program"), "SCADA": ("handle", "alarms"), "P.Eng": ("hold", "registration"),
    "APEGA": ("with", "and"), "Maximo": ("used", None)}


def term_spans(path, present: list[str]) -> dict:
    """whisper-1 word timestamps -> how long each term takes to say, and what a recogniser wrote for it."""
    with path.open("rb") as f:
        r = t10.client.audio.transcriptions.create(model="whisper-1", file=f, response_format="verbose_json",
                                                   timestamp_granularities=["word"], temperature=0)
    words = [(re.sub(r"[^a-z0-9.]", "", w.word.lower()), w.start, w.end, w.word) for w in r.words]
    out = {}
    for t in present:
        before, after = ANCHORS[t]
        try:
            i = next(k for k, w in enumerate(words) if w[0].strip(".") == before)
            j = next((k for k, w in enumerate(words) if k > i and after and w[0].strip(".") == after), len(words))
            seg = words[i + 1:j]
            if t == "Maximo":
                seg = seg[:1]
            out[t] = {"span_s": round(seg[-1][2] - seg[0][1], 2), "whisper_wrote": " ".join(w[3] for w in seg)}
        except (StopIteration, IndexError):
            out[t] = {"span_s": None, "whisper_wrote": None}
    return out


def evaluate(case: str, pcms: list[bytes]) -> dict:
    res = {}
    for n, (text, pcm) in enumerate(zip([T1, T2], pcms), start=1):
        p = HINT_DIR / f"{case}_{n}.wav"
        h.write_wav(p, pcm, 24000)
        present = [t for t in TARGET_TERMS if t in text]
        j = t10.judge(p, present)
        a = t10.asr(p)
        spans = term_spans(p, present)
        res[f"T{n}"] = {"spans": spans, "bytes": len(pcm), "seconds": round(len(pcm) / 48000, 2),
                        "respelling": {t: (j.get(t) or {}) for t in present}, "asr": a,
                        "leaked_markup": bool(re.search(r"phoneme|pipe|angle|<<|\|", a, re.I))}
    print(case, json.dumps({k: {t: (v2.get("respelling"), v["spans"][t]["span_s"], v["spans"][t]["whisper_wrote"])
                                for t, v2 in v["respelling"].items()} | {"secs": v["seconds"]}
                            for k, v in res.items()}, ensure_ascii=False))
    return res


async def main() -> None:
    h.attach_log_file("11_pron_hints")
    results = {"targets": TARGET, "cases": {}}
    cv = b.voices("cartesia")["Harlow"]
    results["cases"]["cartesia_baseline"] = evaluate("cartesia_baseline", await speak("cartesia", cv, [T1, T2]))

    soundslike = create_dict("spike-soundslike", [{"text": t, "pronunciation": TARGET[t]} for t in TARGET_TERMS])
    ipa_items = [{"text": t, "pronunciation": format_cartesia_pronunciation(t, IPA[t])} for t in TARGET_TERMS]
    print("IPA items:", [i["pronunciation"] for i in ipa_items])
    ipa_dict = create_dict("spike-ipa", ipa_items)
    try:
        for tag, pid in (("cartesia_dict_soundslike", soundslike), ("cartesia_dict_ipa", ipa_dict)):
            results["cases"][tag] = evaluate(tag, await speak("cartesia", cv, [T1, T2], extra={"pronunciation_dict_id": pid}))

        def inline(text: str) -> str:
            for t in TARGET_TERMS:
                text = re.sub(re.escape(t) if "." in t else rf"\b{t}\b", format_cartesia_pronunciation(t, IPA[t]), text)
            return text

        results["cases"]["cartesia_inline_ipa"] = evaluate("cartesia_inline_ipa",
                                                           await speak("cartesia", cv, [inline(T1), inline(T2)]))
    finally:
        results["cartesia_dict_delete_status"] = [delete_dict(soundslike), delete_dict(ipa_dict)]

    fv = b.voices("fish")["Ethan"]
    results["cases"]["fish_baseline"] = evaluate("fish_baseline", await speak("fish", fv, [T1, T2]))

    async def fish_tags(text: str, _agg) -> str:
        for t in TARGET_TERMS:
            tag = f"<|phoneme_start|>{ARPA[t]}<|phoneme_end|>"
            text = re.sub(re.escape(t) if "." in t else rf"\b{t}\b", tag, text)
        return text

    results["cases"]["fish_phoneme_tags"] = evaluate("fish_phoneme_tags",
                                                      await speak("fish", fv, [T1, T2], transform=fish_tags))

    results["cases"]["realtime_baseline"] = evaluate("realtime_baseline", await realtime_read("marin", [T1, T2]))
    instr = ("Pronounce these terms exactly: " + "; ".join(f"{t} = '{TARGET[t]}'" for t in TARGET_TERMS) + ".")
    results["cases"]["realtime_prompt_hint"] = evaluate("realtime_prompt_hint", await realtime_read("marin", [T1, T2], instr))
    (h.OUT / "11_pron_hints.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
