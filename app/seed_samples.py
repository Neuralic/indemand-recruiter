"""Build the 3 simulated sample candidates (strong, borderline, weak) and write app/seed/samples.json.

Claude plays BOTH sides: Avery (using the live Vapi assistant's own system prompt and first message, fetched with the private key, so the
questions match the real interview) and a fictional candidate from a scripted persona. The resulting transcripts then go through the SAME
scorecard function as a live call (app.scorecard.generate). They are labelled "Sample candidate (simulated)" in the data and everywhere in the UI.
No audio exists for them.

usage:  .venv-app/Scripts/python.exe -m app.seed_samples            (needs ANTHROPIC_API_KEY, VAPI_PRIVATE_KEY, VAPI_ASSISTANT_ID in .env)
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time

import httpx

import app.config as cfg
from app import scorecard
from role_config import load_role

END_MARK = "[END_CALL]"
SIM_NOTE = (f"\n\n(Simulation note, not part of your instructions to the candidate: this is a text rehearsal. Write only the words you would say aloud: "
            f"never a tool call, JSON, a code block or the word endCall. When the interview is complete, say your goodbye in words and end that "
            f"same message with the exact marker {END_MARK}.)")

_JSON_OBJ = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")
_FENCE = re.compile(r"```.*?```|```", re.S)


def clean_avery(text: str) -> tuple[str, bool]:
    """The rehearsal model sometimes writes the endCall tool as text. Strip tool-call noise; -> (spoken words, ended-by-marker)."""
    ended = END_MARK in text
    t = _FENCE.sub(" ", text.replace(END_MARK, " "))
    t = _JSON_OBJ.sub(" ", t)
    t = re.sub(r"\bendCall\b", " ", t, flags=re.I)
    return " ".join(t.split()), ended

PERSONAS = [
    {"kind": "strong", "name": "Alex Morgan", "first": "Alex", "brief": """You are Alex Morgan, a fictional candidate. 14 years in industrial electrical engineering; P.Eng. in good standing with APEGA for 9 years; electrical engineering degree from a Canadian university; Canadian citizen; based in Edmonton.
Currently a senior electrical engineer at an oil and gas processing facility, leading 5 people (3 engineers, 1 engineer-in-training, 1 designer). Why looking: want formal people leadership and more project ownership.
Concrete stories you tell when asked: redesigned a 600-volt motor control centre lineup and variable frequency drive upgrade for a gas plant, saved 3 weeks on a 6-week shutdown and came in 4 percent under a 1.8 million dollar budget; programmable logic controller migration on a compressor station with your own commissioning plan; Class I Division 2 area classification reviews under the Canadian Electrical Code and API 500 and 505; you stamp roughly 40 drawing sets a year and review each personally; you mentored an engineer-in-training through her professional engineer application and she passed first try; you handled an underperforming designer with a written plan and he improved in 2 months.
Logistics: expect 165 to 180 thousand dollars base; notice 4 weeks; fine with travel up to 20 percent; based in Edmonton, no relocation.
Style: clear, specific, confident, gives numbers and what YOU personally did. Natural spoken sentences, 2 to 5 sentences per answer. Ask one or two sensible questions about the role at the end (for example who the client is and what the pay band is)."""},
    {"kind": "borderline", "name": "Jordan Ellis", "first": "Jordan", "brief": """You are Jordan Ellis, a fictional candidate. 9 years in electrical engineering; P.Eng. with APEGA for 3 years; electrical engineering degree; permanent resident, eligible to work in Canada; based in Calgary and willing to relocate to Edmonton.
Mostly design work for industrial clients (power distribution, some motor control centres). Led a team informally: project lead for 2 junior engineers, no formal supervisor title. Why looking: a plateau at the current consulting firm.
You give decent but sometimes generic answers: good on power systems design with one real example (a 4160 volt substation upgrade) but vague on controls and programmable logic controller work ("I work with the controls team"), no hazardous-location project of your own beyond reviewing others' work, you stamp drawings but talk about the process only loosely. On leadership you stay general ("I try to be supportive and give people room") until probed, then give one thin example. Few numbers; you say "we" a lot.
Logistics: expect 140 to 155 thousand dollars; notice 3 weeks; travel is fine up to 20 percent.
Style: friendly, a little hesitant, 2 to 4 sentences per answer. Ask about team size and whether the role is remote."""},
    {"kind": "weak", "name": "Casey Reid", "first": "Casey", "brief": """You are Casey Reid, a fictional candidate. 6 years in industry as an electrical technologist and then junior engineer; electrical engineering technology diploma, NOT an engineering degree; engineer-in-training with APEGA, NOT a professional engineer and no timeline for it; Canadian citizen; based in Red Deer.
You have never stamped drawings and never led a team (you "coordinated some contractors"). Experience is installing and troubleshooting motor control centres and some programmable logic controller changes under supervision. Answers are short, generic and use "we" ("we followed the code", "we did a lot of troubleshooting"). You cannot name specific codes beyond saying "the electrical code". When probed you give a single vague example with no numbers.
Why looking: want a pay increase and a better title. Logistics: expect 120 thousand dollars; notice 2 weeks; travel is fine.
Style: polite but brief, 1 to 3 sentences per answer, sometimes says "I'm not sure". Ask whether the job is really available right away."""},
]


def _text(resp) -> str:
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def _say(client, model: str, system: str, history: list[tuple[str, str]], who: str) -> str:
    """history: [(speaker, text)] with speaker in avery|candidate; `who` is the speaker we now generate. Returns the next utterance."""
    mine = "assistant"
    msgs: list[dict] = []
    for sp, tx in history:
        role = mine if sp == who else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += " " + tx
        else:
            msgs.append({"role": role, "content": tx})
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(The call has just connected.)"})
    if msgs[-1]["role"] != "user":
        msgs.append({"role": "user", "content": "(continue)"})
    r = await client.messages.create(model=model, max_tokens=1200, system=system, messages=msgs, output_config={"effort": "low"})
    return _text(r)


async def simulate(client, model: str, avery_system: str, first_message: str, persona: dict, max_turns: int = 44) -> list[dict]:
    """Run the text rehearsal -> Vapi-shaped turns [{speaker, text, t_start, t_end}] with plausible spoken timing."""
    avery_sys = re.sub(r"\{\{[^}]*name[^}]*\}\}", persona["first"], avery_system, flags=re.I) + SIM_NOTE
    cand_sys = (persona["brief"] + "\n\nYou are on a first-round phone screening with an AI interviewer called Avery. Reply only with what you would say aloud, "
                "no stage directions, no markdown. Never reveal that you are fictional or an AI.")
    history: list[tuple[str, str]] = []
    first = re.sub(r"\{\{[^}]*name[^}]*\}\}", persona["first"], first_message, flags=re.I).replace(END_MARK, "").strip()
    if first:
        history.append(("avery", first))
    for _ in range(max_turns):
        if not history or history[-1][0] == "avery":
            history.append(("candidate", await _say(client, model, cand_sys, history, "candidate")))
        a, ended = clean_avery(await _say(client, model, avery_sys, history, "avery"))
        if a:
            history.append(("avery", a))
        if ended:
            break
    turns, t = [], 3.0
    for sp, tx in history:
        words = len(tx.split())
        dur = words / (2.7 if sp == "avery" else 2.4)
        turns.append({"speaker": "interviewer" if sp == "avery" else "candidate", "text": tx, "t_start": round(t, 1), "t_end": round(t + dur, 1)})
        t += dur + (0.9 if sp == "avery" else 1.4)
    return turns


async def fetch_avery() -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{cfg.VAPI_API_BASE}/assistant/{cfg.VAPI_ASSISTANT_ID}", headers={"Authorization": f"Bearer {cfg.VAPI_PRIVATE_KEY}"})
    if r.status_code >= 400:
        raise SystemExit(f"Could not fetch the assistant from Vapi (HTTP {r.status_code}). Check VAPI_PRIVATE_KEY and VAPI_ASSISTANT_ID.")
    a = r.json()
    system = "\n\n".join(m.get("content", "") for m in (a.get("model") or {}).get("messages", []) if m.get("role") == "system")
    if not system.strip():
        raise SystemExit("The assistant has no system prompt in model.messages; cannot rehearse Avery faithfully.")
    return system, a.get("firstMessage") or ""


async def main() -> None:
    missing = [n for n in ("ANTHROPIC_API_KEY", "VAPI_PRIVATE_KEY", "VAPI_ASSISTANT_ID") if not getattr(cfg, n)]
    if missing:
        raise SystemExit("Missing in .env: " + ", ".join(missing))
    client = scorecard.make_client()
    role = load_role(cfg.LIVE_ROLE_ID)
    system, first = await fetch_avery()
    print(f"Avery prompt loaded ({len(system)} chars); model {cfg.ANTHROPIC_MODEL}")

    async def one(p: dict) -> dict:
        turns = await simulate(client, cfg.ANTHROPIC_MODEL, system, first, p)
        res = await scorecard.generate(role, turns, "simulated", cfg.SAMPLE_LABEL, client=client)
        if not res.ok:
            raise SystemExit(f"{p['kind']}: scorecard failed: {res.error}")
        print(f"  {p['kind']:10s} {len(turns):3d} turns -> {res.card['overall_score']:3d} {res.card['recommendation']}"
              f" (quotes dropped {res.card['quote_check']['dropped']}/{res.card['quote_check']['proposed']})")
        return {"id": f"sample-{p['kind']}", "role_id": cfg.LIVE_ROLE_ID, "candidate_name": p["name"], "label": cfg.SAMPLE_LABEL, "sample_kind": p["kind"],
                "turns": turns, "card": res.card, "created_at": int(time.time()), "duration_s": turns[-1]["t_end"], "generated_in_s": round(res.seconds, 1)}

    samples = await asyncio.gather(*(one(p) for p in PERSONAS))
    cfg.SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg.SEED_FILE.write_text(json.dumps({"note": "Simulated candidates. Transcripts written by Claude playing Avery and a fictional persona. No real people.",
                                         "samples": samples}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", cfg.SEED_FILE)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
