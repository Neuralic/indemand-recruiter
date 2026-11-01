"""Phase 0b: pronunciation log for the 9 terms across every arm/voice clip (I cannot listen, so two machine proxies).

Proxy 1 (ASR): gpt-4o-transcribe, NO prompt, on each clip -> the spelling a recogniser hears for each term.
Proxy 2 (audio judge): gpt-audio-1.5 given the clip and asked, per term it hears, for a phonetic respelling and whether the
  speaker said it as a word or spelled it letter by letter. CALIBRATED first on OpenAI-TTS clips whose pronunciation I
  control ("calibrate" mode): if the judge cannot tell 'SCADA' (word) from 'S C A D A' (letters) it is useless.
Neither proxy replaces David's ears; out/bakeoff/**/*.wav is the ground truth.

usage: 10_pronunciation.py calibrate | clips [arm/voice ...]
"""

import base64
import io
import json
import re
import sys
import wave
from pathlib import Path

from openai import OpenAI

import bakeoff_common as b
import harness as h

client = OpenAI(api_key=h.api_key())
JUDGE_MODEL = "gpt-audio-1.5"
JUDGE_PROMPT = (
    "You are a pronunciation auditor. You will hear a short clip of a voice agent. For EACH of these terms that is "
    "spoken in the clip: {terms}, report how the speaker actually pronounced it. Return JSON only, shaped as "
    '{{"<term>": {{"style": "word|letters|mixed", "respelling": "<hyphenated syllables, STRESSED in caps, e.g. uh-PEG-uh>"}}}}. '
    "'letters' means each letter is spoken separately (e.g. 'S C A D A' -> es-see-ay-dee-ay). Omit terms you do not hear. "
    "Describe the sounds you hear, not the dictionary pronunciation. Do not guess a term that is not in the clip."
)


def wav_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def judge(path: Path, terms: list[str]) -> dict:
    r = client.chat.completions.create(
        model=JUDGE_MODEL, modalities=["text"], temperature=0,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": JUDGE_PROMPT.format(terms=", ".join(terms))},
            {"type": "input_audio", "input_audio": {"data": wav_b64(path), "format": "wav"}}]}])
    txt = r.choices[0].message.content or ""
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else {"_raw": txt}
    except json.JSONDecodeError:
        return {"_raw": txt}


def asr(path: Path) -> str:
    with path.open("rb") as f:
        return client.audio.transcriptions.create(model="gpt-4o-transcribe", file=f, temperature=0).text


def terms_in(text: str) -> list[str]:
    return [t for t in b.TERMS if t in text]


def asr_form(term: str, transcript: str) -> str:
    """The span of the transcript where `term` should be: the term itself if present, else the nearest token window."""
    if term in transcript:
        return term
    return "(not found verbatim)"


def calibrate() -> None:
    """Known pronunciations via OpenAI TTS: the judge must separate word vs letters."""
    cases = {
        "scada_word": ("Check the SCADA screen.", "word"),
        "scada_letters": ("Check the S. C. A. D. A. screen.", "letters"),
        "mtbf_letters": ("Our M. T. B. F. improved.", "letters"),
        "mtbf_word": ("Our emtibif improved.", "word"),
        "loto_word": ("We run the LOTO audit.", "word"),
        "loto_letters": ("We run the L. O. T. O. audit.", "letters"),
        "apega_a": ("Are you registered with Ah-PEG-ah?", "word"),
        "apega_b": ("Are you registered with Ape-ee-gah?", "word"),
    }
    out = {}
    for k, (text, expect) in cases.items():
        pcm = h.synth(text, voice="onyx")
        p = h.OUT / "audio" / f"calib_{k}.wav"
        h.write_wav(p, pcm, 24000)
        term = {"scada": "SCADA", "mtbf": "MTBF", "loto": "LOTO", "apega": "APEGA"}[k.split("_")[0]]
        res = judge(p, [term])
        got = res.get(term, {})
        out[k] = {"text": text, "expected_style": expect, "judge": got, "asr": asr(p),
                  "pass": isinstance(got, dict) and got.get("style") == expect}
        print(k, out[k])
    n = sum(v["pass"] for v in out.values())
    out["_summary"] = f"{n}/{len(cases)} style calls matched the known pronunciation"
    print(out["_summary"])
    (h.OUT / "10_pron_calibration.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


def clips(filters: list[str]) -> None:
    rows = []
    for arm_dir in sorted((b.BAKE).iterdir()):
        if arm_dir.name not in ("A", "B", "C"):
            continue
        for voice_dir in sorted(arm_dir.iterdir()):
            key = f"{arm_dir.name}/{voice_dir.name}"
            if filters and key not in filters:
                continue
            for wav in sorted(voice_dir.glob("[0-9][0-9]_*.wav")):
                line_key = wav.stem[3:]
                text = dict(b.LINES)[line_key]
                present = [t for t in b.TERMS if t in text]
                if not present:
                    continue
                transcript = asr(wav)
                j = judge(wav, present)
                row = {"arm": arm_dir.name, "voice": voice_dir.name, "line": line_key, "terms": present,
                       "asr": transcript, "judge": j}
                rows.append(row)
                print(key, line_key, {t: (j.get(t) or {}).get("respelling") for t in present})
    path = h.OUT / ("10_pron_clips.json" if not filters else f"10_pron_clips_{b.slug('_'.join(filters))}.json")
    path.write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    h.attach_log_file("10_pronunciation")
    if sys.argv[1] == "calibrate":
        calibrate()
    else:
        clips(sys.argv[2:])
