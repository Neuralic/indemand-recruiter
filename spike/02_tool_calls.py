"""Phase 0 / item 2: is function calling reliable on gpt-realtime-2.1 through Pipecat?

REACTIVE scripted candidate (attempt 1, a rigid fixed script, was invalid - it ran ahead of the bot's stage order;
see out/attempt1_rigid_script_INVALID/). The candidate answers whatever the bot just asked, by keyword rules, and each
answer carries the tool call(s) the §5 design REQUIRES for it. Window = from the moment the candidate starts a turn
until the next one starts. Extras are listed, not penalised; argument validity (enums, stage order) is checked.

usage: 02_tool_calls.py [model] [reasoning_effort|none] [tag] [sync|async]
"""

import asyncio
import json
import re
import sys
import time

from loguru import logger

import harness as h
import interview_spec as spec
from pipecat.frames.frames import LLMRunFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gpt-realtime-2.1"
EFFORT = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "none" else None
TAG = sys.argv[3] if len(sys.argv) > 3 else "run1"
ASYNC = len(sys.argv) > 4 and sys.argv[4] == "async"
NAME = f"02_tool_calls_{MODEL}_{EFFORT or 'default'}_{'async' if ASYNC else 'sync'}_{TAG}"
h.attach_log_file(NAME)

MAX_TURNS = 18

COMPETENCY_ANSWERS = [  # (line, requires log_evidence)
    ("I led our reliability program at a gas processing facility. We brought unplanned downtime down from nine "
     "percent to four percent in two years by rolling out RCM and condition monitoring, and I personally ran the "
     "weekly reliability review with operations.", True),
    ("Yeah, I'd say I manage that pretty well. We've never really had issues.", True),  # deliberately vague -> probe
    ("For example, we ran monthly LOTO audits and our recordable rate dropped from 2.1 to 0.6 over eighteen months. "
     "I signed off every arc flash study myself.", True),
    ("I supervised a team of twelve electricians and instrumentation techs. I restructured our shifts and brought "
     "overtime down by thirty percent while keeping PM compliance above ninety five percent in Maximo.", True),
    ("On our MCC replacement project I owned the scope, a three point two million dollar budget, and we landed it "
     "two weeks early during a planned turnaround.", True),
]


class Policy:
    """Answers whatever the bot just asked."""

    def __init__(self) -> None:
        self.comp_i = 0
        self.consented = False
        self.motivation_done = False
        self.background_done = False
        self.asked: set[str] = set()
        self.questions_done = False

    def reply(self, bot: str) -> tuple[str, set[str], str]:
        b = bot.lower()
        if self.questions_done and re.search(r"anything else|that's everything|wrap|any other|next steps|thank", b):
            return "No, that's everything. Thanks so much for your time.", {"end_interview"}, "goodbye"
        if not self.questions_done and re.search(
                r"questions (for|about)|any questions|anything you'd like to ask|ask me anything|questions you have"
                r"|questions of your own|ask your questions|ask (me|any)|if you have (one|any)|go ahead", b):
            self.questions_done = True
            return ("I do have a couple of questions. What's the bonus structure, and who does the role report to?",
                    {"flag_for_recruiter"}, "candidate_questions")
        if not self.consented and re.search(r"start|ready|begin|good to go|shall we|okay with|sound good|proceed", b):
            self.consented = True
            return "Yes, that's fine. Let's get started.", {"advance_stage"}, "consent"
        rules = [
            ("work_auth", r"eligible to work|authori[sz]ed to work|legally", "Yes, I'm a Canadian citizen, so I'm legally eligible to work in Canada."),
            ("licence", r"p\.?eng|apega|registration|licen[cs]e|registered", "Yes, I'm a registered P.Eng with APEGA, in good standing."),
            ("salary", r"salary|compensation|base pay|expectation", "I'd be looking for one eighty to two hundred thousand base."),
            ("notice_period", r"notice", "I'd need about eight weeks' notice."),
            ("location", r"relocat|located|location|commut|where are you", "I'm in Calgary right now but I'm happy to relocate to Edmonton."),
        ]
        for key, pat, line in rules:
            if key not in self.asked and re.search(pat, b):
                self.asked.add(key)
                return line, {"record_knockout"}, key
        if not self.motivation_done and re.search(r"open to a move|why|what.s (got|driving)|moving on|looking for a", b):
            self.motivation_done = True
            return ("Honestly the plant I'm at is being sold next year and I want to stay in heavy industry.",
                    set(), "motivation")
        if not self.background_done and re.search(r"background|experience|responsib|supervis|operation|specific", b):
            self.background_done = True
            return ("I've spent eleven years in electrical maintenance and reliability at gas plants, the last four "
                    "as a supervisor.", set(), "background")
        line, needs = COMPETENCY_ANSWERS[self.comp_i % len(COMPETENCY_ANSWERS)]
        self.comp_i += 1
        return line, ({"log_evidence"} if needs else set()), f"competency#{self.comp_i}"


class LoggingRealtime(OpenAIRealtimeLLMService):
    async def _handle_evt_error(self, evt):
        logger.error(f"SERVER ERROR: {evt.error}")
        await super()._handle_evt_error(evt)

    async def _handle_evt_function_call_arguments_done(self, evt):
        item = self._pending_function_calls.get(evt.call_id)
        logger.info(f"SERVER function_call name={getattr(item, 'name', '?')!r} args={evt.arguments}")
        await super()._handle_evt_function_call_arguments_done(evt)


async def main() -> int:
    rec = spec.ToolRecorder()
    sp = events.SessionProperties(
        audio=events.AudioConfiguration(
            input=events.AudioInput(
                transcription=events.InputAudioTranscription(),
                turn_detection=events.SemanticTurnDetection(),
            ),
        ),
    )
    if EFFORT:
        sp.reasoning = events.Reasoning(effort=EFFORT)
    llm = LoggingRealtime(
        api_key=h.api_key(),
        settings=OpenAIRealtimeLLMService.Settings(
            model=MODEL, system_instruction=spec.SYSTEM_PROMPT, session_properties=sp,
        ),
    )
    rec.register(llm, async_tools=ASYNC)
    context = LLMContext([{"role": "developer", "content": "Begin the interview now. The candidate's name is Dana."}],
                         spec.TOOLS)
    transcript: list[dict] = []
    bot_texts: list[str] = []

    async def on_user(agg, m):
        transcript.append({"turn": rec.turn, "who": "user", "text": m.content})

    async def on_asst(agg, m):
        if m.content and m.content.strip():
            bot_texts.append(m.content)
        transcript.append({"turn": rec.turn, "who": "bot", "text": m.content})
        logger.info(f"[turn {rec.turn}] BOT: {m.content}")

    sess, run_task, runner = await h.open_session(
        llm, context,
        user_aggregator_handlers={"on_user_turn_message_added": on_user},
        assistant_handlers={"on_assistant_turn_stopped": on_asst},
    )
    await asyncio.wait_for(sess.connected.wait(), 30)
    t_run = time.time()
    await sess.worker.queue_frames([LLMRunFrame()])
    first, last = await h.wait_bot_turn(sess.candidate, timeout=40, quiet_s=2.5)
    assert first, "no greeting audio"
    greet_latency = first - t_run
    logger.info(f"greeting first audio {greet_latency:.2f}s after LLMRunFrame")

    pol = Policy()
    turns = []
    for i in range(1, MAX_TURNS + 1):
        await asyncio.sleep(0.6)  # let the aggregator flush the bot text
        last_bot = " ".join(bot_texts[-2:]) if bot_texts else ""
        line, required, kind = pol.reply(last_bot)
        rec.turn = i
        t_start = time.time()
        dur = await sess.candidate.asay(line, lead_silence_s=0.3)
        t_end_speech = t_start + 0.3 + dur
        logger.info(f"[turn {i}] CANDIDATE ({kind}) <- bot said: ...{last_bot[-110:]!r}\n          says: {line}")
        await asyncio.sleep(dur + 0.5)
        first, last = await h.wait_bot_turn(sess.candidate, timeout=60, quiet_s=2.5)
        await asyncio.sleep(1.5)  # let trailing tool calls land in this window
        turns.append({"turn": i, "kind": kind, "line": line, "required": sorted(required), "t_start": t_start,
                      "reply_latency_s": round(first - t_end_speech, 2) if first else None})
        if kind == "goodbye":
            break

    await asyncio.sleep(2)
    # ---- scoring: window i = [t_start_i, t_start_{i+1}) in the recorder's clock ----
    starts = [t["t_start"] - rec.t0 for t in turns] + [1e9]
    rows = []
    hit = req_total = 0
    for idx, t in enumerate(turns):
        fired = [c.name for c in rec.calls if starts[idx] <= c.t < starts[idx + 1]]
        req = set(t["required"])
        got = req & set(fired)
        hit += len(got)
        req_total += len(req)
        rows.append({**{k: v for k, v in t.items() if k != "t_start"}, "fired": fired, "ok": got == req})
    invalid = [c for c in rec.calls if not c.ok]
    result = {
        "model": MODEL, "reasoning_effort": EFFORT, "async_tools": ASYNC, "greeting_latency_s": round(greet_latency, 2),
        "required_total": req_total, "required_hit": hit, "total_calls": len(rec.calls),
        "invalid_calls": [vars(c) for c in invalid], "per_turn": rows, "final_stage": rec.stage,
        "calls": [vars(c) for c in rec.calls], "transcript": transcript,
    }
    (h.OUT / f"{NAME}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    summary = [f"{NAME}: required hit {hit}/{req_total}; total calls {len(rec.calls)}; invalid {len(invalid)}; "
               f"final stage {rec.stage}; greeting {greet_latency:.2f}s"]
    for r in rows:
        summary.append(f"  turn {r['turn']:>2} {r['kind']:<16} required={r['required']} fired={r['fired']} "
                       f"latency={r['reply_latency_s']} -> {'OK' if r['ok'] else 'MISS'}")
    for ln in summary:
        logger.info(ln)
        print(ln)
    await h.close_session(sess, run_task, runner)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
