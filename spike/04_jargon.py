"""Phase 0 / item 4: jargon test. Do the §8 domain_terms survive transcription?

28 natural sentences (2 per term, two TTS voices) are transcribed:
  Part A  'api'  : OpenAI audio.transcriptions, model x {no prompt, domain prompt}  (fast model comparison)
  Part B  'live' : the REAL path - spoken over SmallWebRTC into OpenAIRealtimeLLMService with a chosen
                   input-transcription config; reads conversation.item.input_audio_transcription.completed.

A term is a HIT if its canonical form appears in the transcript after stripping case/punctuation/spaces
('P.Eng.' == 'p eng' == 'peng').  LENIENT additionally accepts the spelled-out long form (lockout tagout...).
Caveat: TTS speech is cleaner than a real mic, so every number here is an UPPER BOUND on accuracy.

usage: 04_jargon.py api
       04_jargon.py live <transcription_model> <prompt|noprompt>
"""

import asyncio
import io
import json
import os
import re
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from loguru import logger
from openai import OpenAI

import harness as h
import interview_spec as spec

# (canonical term, spoken-form sentence, accepted long-form alternates for LENIENT)
ITEMS = [
    ("APEGA", "I'm a registered professional engineer with Apega here in Alberta.", []),
    ("APEGA", "My Apega registration is in good standing and I renew it every year.", []),
    ("P.Eng", "I got my P Eng about nine years ago after four years of work experience.", ["professionalengineer"]),
    ("P.Eng", "Most supervisors on our site are a P Eng, and I sign off the electrical drawings.", ["professionalengineer"]),
    ("CEC", "All our field modifications have to comply with the C E C before we energize anything.", ["canadianelectricalcode"]),
    ("CEC", "I teach the new technicians how to read the C E C rules for hazardous locations.", ["canadianelectricalcode"]),
    ("arc flash", "I signed off every arc flash study and updated the labels on the equipment.", []),
    ("arc flash", "We reduced our arc flash incident energy by changing the protection settings.", []),
    ("MCC", "We replaced the old M C C buckets during the turnaround.", ["motorcontrolcenter"]),
    ("MCC", "The M C C room had thermography done every quarter.", ["motorcontrolcenter"]),
    ("switchgear", "I managed the medium voltage switchgear overhaul at the compressor station.", []),
    ("switchgear", "The switchgear failed its insulation test, so we had to isolate the whole bus.", []),
    ("PLC", "Our P L C code was rewritten to add better fault diagnostics.", ["programmablelogiccontroller"]),
    ("PLC", "I troubleshoot a P L C and drive faults, mostly on conveyor lines.", ["programmablelogiccontroller"]),
    ("SCADA", "The Scada upgrade gave operations real time visibility of the substation.", []),
    ("SCADA", "I worked with the Scada vendor to migrate our alarms to the new system.", []),
    ("LOTO", "We run monthly Loto audits and every crew has to be trained on it.", ["lockouttagout", "lockoutandtagout", "lockout"]),
    ("LOTO", "After the Loto incident last year I rebuilt our isolation procedures.", ["lockouttagout", "lockoutandtagout", "lockout"]),
    ("CMMS", "Our C M M S had no preventive maintenance history so I rebuilt the asset hierarchy.", ["computerizedmaintenance"]),
    ("CMMS", "Work orders in the C M M S were closed out without any failure codes.", ["computerizedmaintenance"]),
    ("SAP PM", "I configured S A P P M for our maintenance plans and task lists.", ["sapplantmaintenance"]),
    ("SAP PM", "We ran everything through S A P P M and reported on backlog weekly.", ["sapplantmaintenance"]),
    ("Maximo", "I've been a Maximo administrator for about six years.", []),
    ("Maximo", "We migrated from spreadsheets to Maximo and compliance went up to ninety five percent.", []),
    ("RCM", "We rolled out R C M on the critical compressors and cut unplanned downtime.", ["reliabilitycenteredmaintenance", "reliabilitycentredmaintenance"]),
    ("RCM", "I facilitated the R C M workshops with operations and maintenance together.", ["reliabilitycenteredmaintenance", "reliabilitycentredmaintenance"]),
    ("MTBF", "We improved M T B F on the pumps from eight months to twenty two months.", ["meantimebetweenfailure"]),
    ("MTBF", "I track M T B F and downtime for every critical asset on the dashboard.", ["meantimebetweenfailure"]),
]
VOICES = ["onyx", "nova"]  # alternate per item


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def score(transcripts: list[str]) -> dict:
    strict = lenient = 0
    misses = []
    per_term: dict[str, list[bool]] = {}
    for (term, sentence, alts), tr in zip(ITEMS, transcripts):
        c = compact(tr)
        s_hit = compact(term) in c
        l_hit = s_hit or any(a in c for a in alts)
        strict += s_hit
        lenient += l_hit
        per_term.setdefault(term, []).append(s_hit)
        if not s_hit:
            misses.append({"term": term, "said": sentence, "heard": tr})
    consistently_wrong = [t for t, hits in per_term.items() if not any(hits)]
    return {"n": len(ITEMS), "strict_hits": strict, "lenient_hits": lenient,
            "strict_pct": round(100 * strict / len(ITEMS), 1), "lenient_pct": round(100 * lenient / len(ITEMS), 1),
            "terms_consistently_wrong_strict": consistently_wrong, "misses": misses}


DEGRADED = os.environ.get("JARGON_DEGRADE") == "1"  # 10 dB SNR white noise + 8 kHz band-limit


def clip_wav_bytes(i: int) -> bytes:
    pcm = h.synth(ITEMS[i][1], voice=VOICES[i % 2])
    if DEGRADED:
        pcm = h.degrade(pcm, snr_db=10.0, seed=i)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


DOMAIN_PROMPT = ("Industrial electrical and reliability interview. Terms: " + ", ".join(spec.DOMAIN_TERMS) + ".")


def run_api() -> None:
    client = OpenAI(api_key=h.api_key())
    clips = [clip_wav_bytes(i) for i in range(len(ITEMS))]
    results = {}
    for model in ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gpt-transcribe"]:
        for use_prompt in (False, True):
            def one(i):
                kw = {"prompt": DOMAIN_PROMPT} if use_prompt else {}
                try:
                    r = client.audio.transcriptions.create(model=model, file=(f"c{i}.wav", clips[i], "audio/wav"),
                                                           language="en", **kw)
                    return r.text
                except Exception as e:  # noqa: BLE001
                    return f"<<ERROR {type(e).__name__}: {str(e)[:120]}>>"
            t0 = time.time()
            with ThreadPoolExecutor(8) as ex:
                trs = list(ex.map(one, range(len(ITEMS))))
            errs = [t for t in trs if t.startswith("<<ERROR")]
            key = f"{model} | {'prompt' if use_prompt else 'noprompt'}"
            if errs and len(errs) == len(trs):
                results[key] = {"error": errs[0]}
                print(f"{key:<42} ALL ERROR  {errs[0]}")
                continue
            sc = score(trs)
            sc["transcripts"] = trs
            results[key] = sc
            print(f"{key:<42} strict {sc['strict_hits']}/{sc['n']} ({sc['strict_pct']}%)  lenient {sc['lenient_hits']}/{sc['n']}  "
                  f"consistently wrong: {sc['terms_consistently_wrong_strict']}  [{time.time()-t0:.0f}s]")
    (h.OUT / f"04_jargon_api{'_degraded' if DEGRADED else ''}.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")


async def run_live(trans_model: str, use_prompt: bool) -> None:
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.services.openai.realtime import events
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    name = f"04_jargon_live_{trans_model}_{'prompt' if use_prompt else 'noprompt'}{'_degraded' if DEGRADED else ''}"
    h.attach_log_file(name)
    got: list[tuple[str, str]] = []  # (item_id, transcript)

    class Cap(OpenAIRealtimeLLMService):
        async def handle_evt_input_audio_transcription_completed(self, evt):
            got.append((evt.item_id, evt.transcript))
            logger.info(f"TRANSCRIPT[{len(got)}] {evt.transcript!r}")
            await super().handle_evt_input_audio_transcription_completed(evt)

    tr = events.InputAudioTranscription(model=trans_model, language="en", prompt=DOMAIN_PROMPT if use_prompt else None)
    llm = Cap(
        api_key=h.api_key(),
        settings=OpenAIRealtimeLLMService.Settings(
            model="gpt-realtime-2.1",
            system_instruction="You are a silent listener. Never speak.",
            session_properties=events.SessionProperties(audio=events.AudioConfiguration(input=events.AudioInput(
                transcription=tr,
                turn_detection=events.SemanticTurnDetection(create_response=False, interrupt_response=False),
            ))),
        ),
    )
    for i, (_, sentence, _a) in enumerate(ITEMS):  # pre-synthesise so nothing blocks the loop mid-session
        h.synth(sentence, voice=VOICES[i % 2])
    context = LLMContext([{"role": "developer", "content": "Listen only."}])
    sess, run_task, runner = await h.open_session(llm, context)
    await asyncio.wait_for(sess.connected.wait(), 30)
    await asyncio.sleep(4)  # let the realtime session come up
    for i, (term, sentence, _) in enumerate(ITEMS):
        dur = await sess.candidate.asay(sentence, voice=VOICES[i % 2], lead_silence_s=0.3, degraded=DEGRADED)
        await asyncio.sleep(dur + 2.2)
    await asyncio.sleep(6)  # trailing transcripts
    await h.close_session(sess, run_task, runner)
    texts = [t for _, t in got]
    logger.info(f"received {len(texts)} transcripts for {len(ITEMS)} sentences")
    if len(texts) != len(ITEMS):
        print(f"WARNING: got {len(texts)} transcripts for {len(ITEMS)} sentences; scoring first {min(len(texts), len(ITEMS))}")
    texts = (texts + [""] * len(ITEMS))[: len(ITEMS)]
    sc = score(texts)
    sc["transcripts"] = texts
    (h.OUT / f"{name}.json").write_text(json.dumps(sc, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{name}: strict {sc['strict_hits']}/{sc['n']} ({sc['strict_pct']}%)  lenient {sc['lenient_hits']}/{sc['n']}  "
          f"consistently wrong: {sc['terms_consistently_wrong_strict']}")
    for m in sc["misses"][:12]:
        print(f"   MISS {m['term']:<8} heard: {m['heard']!r}")


if __name__ == "__main__":
    if sys.argv[1] == "api":
        run_api()
    else:
        asyncio.run(run_live(sys.argv[2], sys.argv[3] == "prompt"))
