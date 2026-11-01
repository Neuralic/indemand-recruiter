"""Phase 0b: objective pronunciation proxy for every clip - per-term spoken duration from whisper-1 word timestamps.

Why: the audio-judge in 10_pronunciation.py is biased (it called word-length 'LOTO' clips 'EL-OH-TEE-OH'; see
out/10_pron_calibration.json 'loto_word'). A spelled-out acronym takes far longer than the same term said as a word, and
whisper's own spelling of what it heard (e.g. 'Lodo', 'P E and G') shows how it sounded. Neither is a substitute for
listening; the table is for David to know WHICH clips to listen to first.
Writes out/10b_pron_spans.json and out/10b_pron_table.md
"""

import json
import re

import bakeoff_common as b
import harness as h
from openai import OpenAI

client = OpenAI(api_key=h.api_key())


def toks(s: str) -> list[str]:
    return [t for t in (re.sub(r"[^a-z0-9]", "", w.lower()) for w in s.split()) if t]


def spans(wav_path, script: str, terms: list[str]) -> dict:
    with wav_path.open("rb") as f:
        r = client.audio.transcriptions.create(model="whisper-1", file=f, response_format="verbose_json",
                                               timestamp_granularities=["word"], temperature=0)
    words = [(re.sub(r"[^a-z0-9]", "", w.word.lower()), w.start, w.end, w.word.strip()) for w in r.words]
    words = [w for w in words if w[0]]
    st = toks(script)
    out = {}
    for t in terms:
        tt = toks(t.replace(".", " ")) if t != "P.Eng" else ["peng"]
        # locate the term in the script tokens
        n = len(tt)
        idx = next((i for i in range(len(st)) if st[i:i + n] == tt or (t == "P.Eng" and st[i] == "peng")), None)
        if idx is None:
            continue
        prev = st[idx - 1] if idx > 0 else None
        nxt = st[idx + n] if idx + n < len(st) else None
        wi = [w[0] for w in words]
        try:
            i = max(k for k, w in enumerate(wi) if w == prev) if prev else -1
            j = min((k for k, w in enumerate(wi) if k > i and w == nxt), default=len(words)) if nxt else len(words)
            seg = words[i + 1:j]
            if t == "Maximo" or not nxt:
                seg = seg[:1] if t == "Maximo" else seg
            out[t] = {"span_s": round(seg[-1][2] - seg[0][1], 2), "whisper_wrote": " ".join(w[3] for w in seg),
                      "n_whisper_words": len(seg)}
        except (ValueError, IndexError):
            out[t] = {"span_s": None, "whisper_wrote": None, "n_whisper_words": 0}
    return out


def main() -> None:
    rows = []
    for arm_dir in sorted(b.BAKE.iterdir()):
        if arm_dir.name not in ("A", "B", "C"):
            continue
        for voice_dir in sorted(arm_dir.iterdir()):
            for wav in sorted(voice_dir.glob("[0-9][0-9]_*.wav")):
                key = wav.stem[3:]
                script = dict(b.LINES)[key]
                present = [t for t in b.TERMS if t in script]
                if not present:
                    continue
                s = spans(wav, script, present)
                rows.append({"arm": arm_dir.name, "voice": voice_dir.name, "line": key, "terms": s})
                print(arm_dir.name, voice_dir.name, key, {t: (v["span_s"], v["whisper_wrote"]) for t, v in s.items()})
    (h.OUT / "10b_pron_spans.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    # markdown table: term x voice -> "span / whisper wrote"
    voices = [f"{r['arm']}/{r['voice']}" for r in rows]
    voices = list(dict.fromkeys(voices))
    md = ["| term | " + " | ".join(voices) + " |", "|---|" + "---|" * len(voices)]
    for t in b.TERMS:
        cells = []
        for v in voices:
            hit = next((r["terms"][t] for r in rows if f"{r['arm']}/{r['voice']}" == v and t in r["terms"]), None)
            cells.append(f"{hit['span_s']}s `{hit['whisper_wrote']}`" if hit else "-")
        md.append(f"| {t} | " + " | ".join(cells) + " |")
    (h.OUT / "10b_pron_table.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
