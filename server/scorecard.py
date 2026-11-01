"""Scorecard worker (CLAUDE.md section 10, UI-SPEC.md section 5).

The model (gpt-5.6-sol, medium reasoning; fallback gpt-5.6-terra) judges the competencies. Everything that can be decided in code is:
the knock-outs (the engine's own verified records), quote verification and timestamps, protected-ground stripping, score ranges, the
recommendation (from the score and the knock-outs), logistics, and the items deferred to Stefanie. A quote that is not found in the
transcript is dropped; a competency left with no verified quote is marked so.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass

import server.config as cfg
from guardrails import redact_protected
from quote_validation import validate_quote
from role_config import RoleConfig

RECOMMENDATIONS = ["Submit", "Hold", "Pass"]
SUBMIT_AT, HOLD_AT = 75, 50  # overall score thresholds; a failed required knock-out caps the recommendation at Hold


class ScorecardError(Exception):
    pass


def schema(role: RoleConfig) -> dict:
    s = {"type": "string"}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["overall_score", "recommendation", "headline", "competencies", "strengths", "risks", "verify_on_call",
                     "candidate_questions_to_answer"],
        "properties": {
            "overall_score": {"type": "integer"},
            "recommendation": {"type": "string", "enum": RECOMMENDATIONS},
            "headline": s,
            "competencies": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "score", "confidence", "quotes", "rationale"],
                "properties": {"id": {"type": "string", "enum": [c.id for c in role.competencies]}, "score": {"type": "integer"},
                               "confidence": {"type": "string", "enum": ["high", "med", "low"]},
                               "quotes": {"type": "array", "items": s}, "rationale": s}}},
            "strengths": {"type": "array", "items": s}, "risks": {"type": "array", "items": s},
            "verify_on_call": {"type": "array", "items": s}, "candidate_questions_to_answer": {"type": "array", "items": s},
        },
    }


INSTRUCTIONS = """You write the recruiter scorecard for a first-round voice screening interview, for a recruiter at an executive search firm who will read it in two minutes and decide whether to call the candidate. Role: {title}. The interview was run by an AI screener; a human reviews every scorecard. Return ONLY the JSON required by the schema.

Use ONLY the transcript and the engine records provided. Never invent facts, employers, numbers or quotes.
- overall_score: integer 0-100. 75+ = a strong candidate worth submitting; 50-74 = mixed, hold; under 50 = weak. Weight the competencies as given.
- competencies: exactly one entry per competency id. score 1-5 (1 = no usable evidence or only generic claims, 2 = thin, 3 = adequate, 4 = strong with a specific example, 5 = exceptional: specific project, own decisions, measurable result). confidence: high / med / low = how much evidence supports the score. The engine probed a vague answer at most once; if its record says the answer stayed vague (assessment_forced_weak), score 1 or 2.
- quotes: 1-2 per competency, copied EXACTLY, word for word, from CANDIDATE lines of the transcript (6-30 words each, one contiguous piece of one turn, including any transcription errors). Never quote the interviewer. If nothing is quotable return an empty list.
- rationale: one or two plain sentences that explain the score from what the candidate actually said.
- headline: one plain line a recruiter would say about this candidate (who they are, strongest point, biggest gap). Third person, no hype.
- strengths: 2-4 short bullets. risks: 2-4 short bullets (gaps, vague areas, failed or unclear knock-outs, anything that needs checking).
- verify_on_call: exactly 3 specific things the recruiter should confirm on a call.
- candidate_questions_to_answer: what the candidate asked that the recruiter still needs to answer (empty if none).
- Refer to the candidate as "the candidate" or "they". Never use he, him, his, she or her: gender is unknown.
- Transcription errors happen (the transcript is automatic speech recognition); judge meaning generously and say so in a risk only if it matters.
- Never mention or infer age, race, colour, ancestry, origin, religion, gender, sexual orientation, marital or family status, pregnancy, disability or source of income, and do not comment on accent or voice. Text marked [unclear] was withheld on purpose: ignore it.

COMPETENCIES (id: name, weight; strong signals; weak signals):
{competencies}
"""


def build_instructions(role: RoleConfig) -> str:
    comps = "\n".join(f"- {c.id}: {c.name}, weight {c.weight}; strong: {'; '.join(c.strong_signals)}; weak: {'; '.join(c.weak_signals)}"
                      for c in role.competencies)
    return INSTRUCTIONS.format(title=role.title, competencies=comps)


def build_input(role: RoleConfig, turns: list[dict], evidence: list[dict], knockouts: list[dict], flags: list[dict], ended: str) -> str:
    tr = "\n".join(f"[{int(t['t_start']) // 60:02d}:{int(t['t_start']) % 60:02d}] {'CANDIDATE' if t['speaker'] == 'candidate' else 'INTERVIEWER'}: {t['text']}"
                   for t in turns)
    ev = "\n".join(f"- {e['competency_id']} ({e['assessment']}{', forced weak: still vague after the one probe' if e['forced_weak'] else ''}): {e['quote']}"
                   for e in evidence) or "(none)"
    ko = "\n".join(f"- {k['knockout_id']} [{k['status']}]{' (required)' if k['required'] else ''}: {k['answer']}" for k in knockouts) or "(none)"
    fl = "\n".join(f"- {f['topic']}: {f['reason']}" for f in flags) or "(none)"
    return (f"INTERVIEW ENDED: {ended}\n\nENGINE EVIDENCE (already verified quotes with the engine's rating):\n{ev}\n\n"
            f"KNOCK-OUTS (engine records):\n{ko}\n\nFLAGS RAISED:\n{fl}\n\nTRANSCRIPT:\n{tr}")


# ---- validation and assembly (all deterministic) --------------------------------------------------------------------------------
def _clean_list(xs, n_max=6) -> list[str]:
    return [redact_protected(" ".join(str(x).split()))[0] for x in (xs or []) if str(x).strip()][:n_max]


def validate_model_output(d: dict, role: RoleConfig) -> dict:
    """Raise ScorecardError on anything out of range or incomplete, so the worker retries instead of showing a broken page."""
    if not isinstance(d.get("overall_score"), int) or not 0 <= d["overall_score"] <= 100:
        raise ScorecardError(f"overall_score out of range: {d.get('overall_score')!r}")
    ids = [c["id"] for c in d.get("competencies", [])]
    if sorted(ids) != sorted(c.id for c in role.competencies):
        raise ScorecardError(f"competencies must be exactly {[c.id for c in role.competencies]}, got {ids}")
    for c in d["competencies"]:
        if not isinstance(c["score"], int) or not 1 <= c["score"] <= 5:
            raise ScorecardError(f"competency score out of range: {c['id']}={c['score']!r}")
        if not c["rationale"].strip():
            raise ScorecardError(f"empty rationale for {c['id']}")
    if len(d.get("verify_on_call", [])) != 3:
        raise ScorecardError(f"verify_on_call must have exactly 3 items, got {len(d.get('verify_on_call', []))}")
    if not d.get("headline", "").strip() or not d.get("strengths") or not d.get("risks"):
        raise ScorecardError("headline, strengths and risks are required")
    return d


def recommendation_for(score: int, knockouts: list[dict]) -> tuple[str, str]:
    """Rule-based, so the same interview never gets two answers: (recommendation, why)."""
    failed = [k["knockout_id"] for k in knockouts if k["required"] and k["status"] == "fail"]
    rec = "Submit" if score >= SUBMIT_AT else "Hold" if score >= HOLD_AT else "Pass"
    if failed and rec == "Submit":
        return "Hold", f"capped at Hold: required knock-out failed ({', '.join(failed)})"
    return rec, ""


def _docs(turns: list[dict]) -> tuple[list[str], list[int]]:
    """Candidate turns, plus adjacent pairs (a quote can span two fragments of one answer). Returns (docs, turn index per doc)."""
    cand = [t for t in turns if t["speaker"] == "candidate"]
    docs = [t["text"] for t in cand] + [f"{cand[i]['text']} {cand[i + 1]['text']}" for i in range(len(cand) - 1)]
    owner = [i for i in range(len(cand))] + [i for i in range(len(cand) - 1)]
    return docs, owner


def verify_quotes(quotes: list[str], turns: list[dict]) -> tuple[list[dict], int]:
    """-> ([{quote (the transcript's own words), t, verified: True}], number dropped). Unverifiable quotes are dropped."""
    cand = [t for t in turns if t["speaker"] == "candidate"]
    docs, owner = _docs(turns)
    out, dropped = [], 0
    for q in quotes:
        if not docs:
            dropped += 1
            continue
        m = validate_quote(q, docs)
        if not m.ok:
            dropped += 1
            continue
        text, t = redact_protected(m.text)[0], cand[owner[m.doc_index]]["t_start"]
        if not any(o["quote"] == text for o in out):
            out.append({"quote": text, "t": round(t, 1), "verified": True})
    return out, dropped


def assemble(d: dict, role: RoleConfig, turns: list[dict], evidence: list[dict], knockouts: list[dict], flags: list[dict],
             versions: dict, label: str) -> dict:
    """Model output (validated) + the engine's records -> the scorecard that is stored and shown."""
    proposed = dropped = 0
    comps = []
    by_id = {c["id"]: c for c in d["competencies"]}
    for rc in role.competencies:
        c = by_id[rc.id]
        proposed += len(c["quotes"])
        ev, drop = verify_quotes(c["quotes"], turns)
        dropped += drop
        note = ""
        if not ev:  # fall back to the engine's own evidence rows for this competency, re-verified against the final transcript
            rows = [e["quote"] for e in evidence if e["competency_id"] == rc.id and e["quote"]]
            ev, _ = verify_quotes(rows[:2], turns)
            note = "model quotes could not be verified; showing the engine's logged evidence" if ev else "no verified quote for this competency"
        confidence = c["confidence"] if ev else "low"
        comps.append({"id": rc.id, "name": rc.name, "weight": rc.weight, "score": c["score"], "confidence": confidence, "evidence": ev,
                      "rationale": redact_protected(c["rationale"].strip())[0], "evidence_note": note})
    kos = [{"id": k["knockout_id"], "question": k.get("question", ""), "required": bool(k["required"]), "status": k["status"],
            "answer": redact_protected(k["answer"] or "")[0], "quote": redact_protected(k["quote"] or "")[0] if k["quote_verified"] else "",
            "quote_verified": bool(k["quote_verified"]), "t": k["t"]} for k in knockouts]
    req = [k for k in kos if k["required"]]
    score = d["overall_score"]
    rec, why = recommendation_for(score, knockouts)
    risks = _clean_list(d["risks"])
    if why:
        risks.insert(0, "A required knock-out was failed: " + ", ".join(k["id"] for k in req if k["status"] == "fail") + ".")
    ko_by = {k["id"]: k for k in kos}
    logistics = {key: {"answer": ko_by[key]["answer"], "quote": ko_by[key]["quote"], "status": ko_by[key]["status"]}
                 for key in ("salary", "notice_period", "location", "travel") if key in ko_by}
    deferred = deferred_to_stefanie(flags)
    return {
        "overall_score": score, "recommendation": rec, "model_recommendation": d["recommendation"],
        "recommendation_note": why, "headline": redact_protected(d["headline"].strip())[0], "label": label,
        "summary_label": "AI-assisted summary for human review",
        "competencies": comps, "knockouts": kos, "knockouts_required_passed": sum(k["status"] == "pass" for k in req), "knockouts_required": len(req),
        "strengths": _clean_list(d["strengths"]), "risks": risks, "verify_on_call": _clean_list(d["verify_on_call"], 3),
        "candidate_questions_to_answer": _clean_list(d["candidate_questions_to_answer"]),
        "logistics": logistics, "deferred_to_stefanie": deferred, "quote_check": {"proposed": proposed, "dropped": dropped},
        "versions": versions, "generated_at": time.time(),
    }


_DEFER = {"client": re.compile(r"\bclient\b", re.I), "benefits": re.compile(r"\bbenefit", re.I),
          "compensation": re.compile(r"\b(salary|pay|compensation|bonus|rate)\b", re.I)}


def deferred_to_stefanie(flags: list[dict]) -> list[dict]:
    """Things the candidate asked that the posting does not answer (client, benefits ...), from the engine's recruiter flags."""
    out, seen = [], set()
    for f in flags:
        blob = f"{f['topic']} {f['reason']}"
        if not re.search(r"candidate (?:asked|question)", f["topic"], re.I):
            continue
        topic = next((k for k, rx in _DEFER.items() if rx.search(blob)), "other")
        detail = redact_protected(f["reason"])[0]
        if (topic, detail) not in seen:
            seen.add((topic, detail))
            out.append({"topic": topic, "detail": detail})
    return out


# ---- the model call ------------------------------------------------------------------------------------------------------------
async def call_model(client, model: str, role: RoleConfig, text: str, timeout: float) -> dict:
    r = await asyncio.wait_for(client.responses.create(
        model=model, instructions=build_instructions(role), input=text, reasoning={"effort": cfg.SCORECARD_EFFORT},
        text={"format": {"type": "json_schema", "name": "scorecard", "schema": schema(role), "strict": True}}), timeout)
    return json.loads(r.output_text)


@dataclass
class Result:
    ok: bool
    card: dict | None = None
    model: str = ""
    attempts: int = 0
    error: str = ""
    seconds: float = 0.0


async def generate(role: RoleConfig, turns: list[dict], evidence: list[dict], knockouts: list[dict], flags: list[dict], ended: str,
                   versions: dict, label: str, client=None, on_attempt=None) -> Result:
    """Two attempts at most: sol, then terra. Anything invalid (range, shape, timeout, API error) counts as a failed attempt."""
    if client is None:
        import os

        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0)
    text = build_input(role, turns, evidence, knockouts, flags, ended)
    t0, err = time.time(), ""
    for n, model in enumerate([cfg.SCORECARD_MODEL, cfg.SCORECARD_FALLBACK][:cfg.SCORECARD_ATTEMPTS], 1):
        if on_attempt:
            on_attempt(n, model)
        try:
            d = validate_model_output(await call_model(client, model, role, text, cfg.SCORECARD_TIMEOUT_S), role)
            card = assemble(d, role, turns, evidence, knockouts, flags, {**versions, "scorecard_model": model,
                                                                         "scorecard_prompt": cfg.SCORECARD_PROMPT_VERSION}, label)
            return Result(True, card, model, n, "", time.time() - t0)
        except Exception as e:  # noqa: BLE001 - every failure mode is one more failed attempt
            err = f"{model}: {type(e).__name__}: {str(e)[:200]}"
    return Result(False, None, "", cfg.SCORECARD_ATTEMPTS, err, time.time() - t0)
