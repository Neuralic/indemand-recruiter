"""Phase 0 / item 7: which OpenAI reasoning model should produce the §10 scorecard?

Calls candidate models via the Responses API with STRICT structured output in the §10 shape, on a REAL transcript
produced by the item-2 run. Measures latency (Phase 3 target: scorecard < 60 s after hangup), reports errors, and
runs the §10 quote check (every quote must exist in the transcript after normalisation).

usage: 07_scorecard_model.py <path-to-02-json> [model ...]
"""

import json
import re
import sys
import time
import unicodedata

from openai import OpenAI

import harness as h
import interview_spec as spec

src = sys.argv[1]
models = sys.argv[2:] or ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"]
data = json.load(open(src, encoding="utf-8"))

transcript_lines = []
for t in data["transcript"]:
    if t["text"] and t["text"].strip():
        transcript_lines.append(f"{'CANDIDATE' if t['who'] == 'user' else 'AVERY'}: {t['text'].strip()}")
transcript = "\n".join(transcript_lines)
evidence = [
    {"t": round(c["t"], 1), **c["args"]} for c in data["calls"] if c["name"] == "log_evidence"
]
knockouts = [c["args"] for c in data["calls"] if c["name"] == "record_knockout"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overall_score", "recommendation", "headline", "competencies", "knockouts", "strengths", "risks",
                 "verify_on_call", "candidate_questions_to_answer"],
    "properties": {
        "overall_score": {"type": "integer"},
        "recommendation": {"type": "string", "enum": ["Submit", "Hold", "Pass"]},
        "headline": {"type": "string"},
        "competencies": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "score", "confidence", "evidence", "rationale"],
            "properties": {
                "id": {"type": "string", "enum": spec.COMPETENCIES},
                "score": {"type": "integer"},
                "confidence": {"type": "string", "enum": ["high", "med", "low"]},
                "evidence": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["quote", "t"],
                    "properties": {"quote": {"type": "string"}, "t": {"type": "number"}}}},
                "rationale": {"type": "string"}}}},
        "knockouts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "status", "answer"],
            "properties": {"id": {"type": "string", "enum": spec.KNOCKOUTS},
                           "status": {"type": "string", "enum": ["pass", "fail", "unclear"]},
                           "answer": {"type": "string"}}}},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "verify_on_call": {"type": "array", "items": {"type": "string"}},
        "candidate_questions_to_answer": {"type": "array", "items": {"type": "string"}},
    },
}

INSTRUCTIONS = (
    "You are scoring a first-round voice screening interview for a boutique executive search firm (role: "
    "Electrical Engineering Supervisor, Edmonton). Score ONLY from the transcript. Every competency score (1-5) must "
    "cite at least one VERBATIM quote copied exactly from a CANDIDATE line. Exclude any protected-ground content "
    "(age, family status, religion, origin, disability, etc.). Write the headline in a warm recruiter's voice. "
    "verify_on_call must have exactly 3 items."
)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().replace("’", "'").replace("‘", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


cand_norm = norm(" ".join(t["text"] for t in data["transcript"] if t["who"] == "user" and t["text"]))
client = OpenAI(api_key=h.api_key())
user_input = (f"ROLE COMPETENCIES: {spec.COMPETENCIES}\n\nTRANSCRIPT:\n{transcript}\n\n"
              f"LOGGED EVIDENCE (from live tool calls):\n{json.dumps(evidence)}\n\n"
              f"KNOCK-OUTS:\n{json.dumps(knockouts)}")
print(f"input chars={len(user_input)} (~{len(user_input)//4} tokens); evidence rows={len(evidence)}")

out = {}
for m in models:
    for effort in ("medium",):
        t0 = time.time()
        try:
            r = client.responses.create(
                model=m, instructions=INSTRUCTIONS, input=user_input,
                reasoning={"effort": effort},
                text={"format": {"type": "json_schema", "name": "scorecard", "schema": SCHEMA, "strict": True}},
            )
            dt = time.time() - t0
            card = json.loads(r.output_text)
            quotes = [e["quote"] for c in card["competencies"] for e in c["evidence"]]
            found = [norm(q) in cand_norm for q in quotes]
            u = r.usage
            row = {"model": m, "effort": effort, "ok": True, "latency_s": round(dt, 1),
                   "tokens_in": u.input_tokens, "tokens_out": u.output_tokens,
                   "reasoning_tokens": getattr(u.output_tokens_details, "reasoning_tokens", None),
                   "overall": card["overall_score"], "rec": card["recommendation"],
                   "quotes": len(quotes), "quotes_verbatim_in_transcript": sum(found),
                   "verify_on_call_n": len(card["verify_on_call"]), "headline": card["headline"],
                   "bad_quotes": [q for q, f in zip(quotes, found) if not f][:3]}
            out[f"{m}:{effort}"] = {"row": row, "card": card}
        except Exception as e:  # noqa: BLE001
            row = {"model": m, "effort": effort, "ok": False, "latency_s": round(time.time() - t0, 1),
                   "error": f"{type(e).__name__}: {str(e)[:300]}"}
            out[f"{m}:{effort}"] = {"row": row}
        print(json.dumps(row, ensure_ascii=False))

(h.OUT / "07_scorecard_model.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
