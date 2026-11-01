"""Interview state machine (CLAUDE.md §6, §14). Pure logic, no Pipecat, no network: every decision that must always hold lives here.

  * One step at a time: welcome -> rapport -> background -> competencies (one step each) -> logistics (one step per knock-out)
    -> candidate_questions -> close. Steps only move forward by one; `close` is unreachable until every REQUIRED knock-out has
    a recorded answer, unless a named forced exit applies (candidate declined / asked for a person / asked to stop, off-topic
    x3, silence, time limit).
  * A vague competency answer is probed EXACTLY once (probe_used). After the probe the step resolves either way: a still-vague
    answer is logged "weak" by code (the evaluator's label is overridden), then the interview moves on.
  * All spoken questions come from the role config (primary_question / probe_if_vague / knock-out questions / script) or the
    fixed lines below. Nothing the realtime model says is a question.
  * Evidence quotes are validated against the candidate's own words (quote_validation.py) before they are stored.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from evaluator import EvalContext, Extraction, Verdict
from guardrails import mentions_protected, redact_protected
from quote_validation import validate_quote
from role_config import RoleConfig
from turn_hygiene import glossary_answer, is_permission_request

STAGES = ["welcome", "rapport", "background", "competencies", "logistics", "candidate_questions", "close"]

DISCLOSURE = ("Hi {name}, I'm Avery, IN DEMAND's AI screening assistant. This is a short first conversation about the {title} role "
              "in Edmonton. IN DEMAND uses AI tools to assist with screening. They support the team and don't replace human judgment "
              "or decision-making. This conversation is recorded so Stefanie's team can review it personally. If you'd prefer to "
              "speak with a person instead, just say so. Shall we start?")
SHALL_WE = "Shall we start?"
CLOSE = "Thanks so much, {name}. Stefanie's team reviews every interview personally, and if it's a fit they'll be in touch. Take care."
LINE_HUMAN = "Absolutely. I'll let Stefanie's team know you'd like to speak with a person. Thanks for your time."
LINE_DECLINE = "No problem at all. I'll let Stefanie's team know. Thanks for your time."
LINE_STOP = "No problem at all. I'll let Stefanie's team know. Thanks for your time."
LINE_OFFTOPIC_END = "I'm going to wrap up here. Thanks for your time."
LINE_REDIRECT = "Let's keep us focused on the role."
LINE_REASK = "Sorry, I didn't quite catch that."
LINE_REASK_KO = "Sorry, I just want to make sure I have that right."
LINE_DEFER = "Stefanie will confirm that with you."
LINE_PERMISSION = "Of course, we'll make time for your questions at the end."
LINE_PERMISSION_AGAIN = "We'll get to your questions at the end."
LINE_GO_AHEAD = "Of course, go ahead."
LINE_LOGISTICS = "That's very helpful. I have a few quick logistics questions now."
SILENCE_LINES = ["Take your time, I'm here whenever you're ready.", "Are you still there?",
                 "I'm not hearing anything, so I'll end our conversation here. Thank you for your time."]
ALWAYS_FLAG = {"client"}  # answered from the posting, but Stefanie should know the candidate asked
MAX_CANDIDATE_QUESTIONS = 3
FORCED_EXITS = {"declined", "consent_unclear", "wants_human", "stop_request", "off_topic", "silence", "time_limit", "error"}


class StageGateError(Exception):
    pass


@dataclass
class Step:
    kind: str  # welcome | rapport | background | competency | knockout | candidate_questions | close
    id: str
    question: str

    @property
    def stage(self) -> str:
        return {"competency": "competencies", "knockout": "logistics"}.get(self.kind, self.kind)


@dataclass
class ExtractJob:
    ctx: EvalContext
    step: Step
    final_specific: bool  # False => any competency evidence from this step is forced to "weak"
    verdict_status: str = ""  # knock-out status chosen on the critical path


@dataclass
class Action:
    speak: str
    kind: str  # question | probe | reask | redirect | defer | answer | close | silence | transition
    end: bool = False
    reason: str = ""
    extract: ExtractJob | None = None


@dataclass
class Interview:
    role: RoleConfig
    first_name: str
    clock: Callable[[], float] = time.monotonic
    time_scale: float = 1.0  # tests/dev: 1.0 = real minutes
    steps: list[Step] = field(default_factory=list)
    idx: int = 0
    started: float = 0.0
    probe_used: set[str] = field(default_factory=set)
    reasks: dict[str, int] = field(default_factory=dict)
    permission_asks: dict[str, int] = field(default_factory=dict)  # "Can I ask a question?" per step: answered once, never looped
    answer_turns: list[str] = field(default_factory=list)
    current_question: str = ""
    post_probe: bool = False
    offtopic_run: int = 0
    cq_count: int = 0
    wrapping: bool = False
    ended: str = ""  # reason, once the interview is over
    history: list[tuple[str, str]] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    rejected_quotes: list[dict] = field(default_factory=list)
    knockouts: dict[str, dict] = field(default_factory=dict)
    flags: list[dict] = field(default_factory=list)
    stage_log: list[dict] = field(default_factory=list)
    protected_volunteered: int = 0  # a count only: the content is deliberately never stored
    _answer_hist: list[int] = field(default_factory=list)  # indices into `history` of the current answer's candidate turns

    def __post_init__(self) -> None:
        r = self.role
        self.steps = ([Step("welcome", "welcome", SHALL_WE), Step("rapport", "rapport", r.script["rapport_question"]),
                       Step("background", "background", r.script["background_question"])]
                      + [Step("competency", c.id, c.primary_question) for c in r.competencies]
                      + [Step("knockout", k.id, k.question) for k in r.knockouts]
                      + [Step("candidate_questions", "candidate_questions", r.script["candidate_questions_prompt"]),
                         Step("close", "close", "")])
        self.started = self.clock()
        self._log_stage("welcome")

    # ---- helpers -------------------------------------------------------------------------------------------------------
    @property
    def step(self) -> Step:
        return self.steps[self.idx]

    @property
    def stage(self) -> str:
        return self.step.stage

    def elapsed(self) -> float:
        return self.clock() - self.started

    def _log_stage(self, note: str = "") -> None:
        self.stage_log.append({"t": round(self.elapsed(), 1), "stage": self.stage, "step": self.step.id, "note": note})

    def flag(self, topic: str, reason: str) -> None:
        self.flags.append({"t": round(self.elapsed(), 1), "topic": topic, "reason": reason})

    def say(self, text: str) -> None:
        self.history.append(("bot", text))

    def opening(self) -> str:
        text = DISCLOSURE.format(name=self.first_name, title=self.role.title)
        self.current_question = SHALL_WE
        self.say(text)
        return text

    def comp(self, cid: str):
        return next(c for c in self.role.competencies if c.id == cid)

    def short_of(self, question: str) -> str:
        """The short form of a pending question, for repeating it mid-interview (a 15-second re-read is never acceptable)."""
        q = (question or "").strip()
        for c in self.role.competencies:
            if q == c.primary_question.strip() and c.short_question:
                return c.short_question
        for k in self.role.knockouts:
            if q == k.question.strip() and k.short_question:
                return k.short_question
        if len(q.split()) <= 24:
            return q
        first = re.split(r"(?<=[.?!])\s+", q)[0]
        words = first.split()
        return first if len(words) <= 24 else " ".join(words[:20]).rstrip(",;:") + "?"

    def required_knockouts_done(self) -> bool:
        return all(k.id in self.knockouts for k in self.role.knockouts if k.required)

    # ---- the stage gate ------------------------------------------------------------------------------------------------
    def _advance(self) -> None:
        """Move exactly one step forward. Entering `close` requires every required knock-out to be recorded."""
        nxt = self.idx + 1
        if nxt >= len(self.steps):
            raise StageGateError("already at the last step")
        if self.steps[nxt].kind == "close" and not self.required_knockouts_done():
            raise StageGateError("STAGE GATE: close before required knock-outs were recorded")
        self.idx = nxt
        self.answer_turns, self.post_probe, self._answer_hist = [], False, []
        self._log_stage()

    def _skip_to_logistics(self) -> None:
        """Time budget only: drop the unasked competencies. Still forward-only, still passes through the gate for `close`."""
        while self.step.kind in ("rapport", "background", "competency", "welcome"):
            self.idx += 1
        self.answer_turns, self.post_probe, self._answer_hist = [], False, []
        self._log_stage("time budget: remaining competencies skipped")

    def force_close(self, reason: str) -> Action:
        """The only way past the gate: a named exit. Logistics being incomplete is itself flagged for Stefanie."""
        if reason not in FORCED_EXITS:
            raise StageGateError(f"unknown forced exit {reason!r}")
        if not self.required_knockouts_done():
            self.flag("logistics incomplete", f"interview ended early ({reason}); required knock-outs not all recorded")
        self.idx = len(self.steps) - 1
        self.ended = reason
        self._log_stage(f"forced close: {reason}")
        line = {"wants_human": LINE_HUMAN, "declined": LINE_DECLINE, "consent_unclear": LINE_DECLINE, "stop_request": LINE_STOP,
                "off_topic": LINE_OFFTOPIC_END, "silence": SILENCE_LINES[2], "time_limit": CLOSE.format(name=self.first_name),
                "error": ""}[reason]
        return Action(line, "close", end=True, reason=reason)

    def _close(self) -> Action:
        self._advance()
        self.ended = "completed"
        return Action(CLOSE.format(name=self.first_name), "close", end=True, reason="completed")

    # ---- input from the call -------------------------------------------------------------------------------------------
    def on_user_turn(self, text: str) -> None:
        self.answer_turns.append(text)
        self._answer_hist.append(len(self.history))
        self.history.append(("user", text))

    def note_late_fragment(self, text: str) -> None:
        """A transcript fragment of speech that ended before the current question was asked: history only, never an answer."""
        self.history.append(("user", text))

    def eval_context(self) -> EvalContext:
        s = self.step
        return EvalContext(step_kind=s.kind, step_id=s.id, question=self.current_question, post_probe=self.post_probe,
                           answer_turns=list(self.answer_turns),
                           history=[h for i, h in enumerate(self.history) if i not in set(self._answer_hist)])

    # ---- time and silence ----------------------------------------------------------------------------------------------
    def time_check(self) -> Action | None:
        """Soft wrap at 13 min (skip remaining competencies), hard close at 16 (CLAUDE.md §5)."""
        m = self.elapsed() / 60.0 / self.time_scale
        if m >= 16 and not self.ended:
            return self.force_close("time_limit")
        if m >= 13 and not self.wrapping:
            self.wrapping = True
            self.flag("time budget", "13-minute soft limit reached; remaining competency questions were skipped")
        return None

    def silence_action(self, level: int) -> Action:
        """level 1 (8 s), 2 (20 s), 3 (45 s => graceful end)."""
        if level >= 3:
            self.flag("candidate silent", "no response for 45 seconds; interview ended")
            return self.force_close("silence")
        line = SILENCE_LINES[level - 1]
        self.say(line)
        return Action(line, "silence")

    # ---- the decision --------------------------------------------------------------------------------------------------
    def decide(self, v: Verdict) -> Action:
        a = self._decide(v)
        if a.speak:
            self.say(a.speak)
        return a

    def _decide(self, v: Verdict) -> Action:
        hard = self.time_check()
        if hard:
            return hard
        s = self.step
        if v.failed:  # evaluator unavailable: never block or double-probe; take the neutral path and tell Stefanie
            self.flag("evaluator unavailable", f"step {s.id}: {v.failed[:120]}")
            v = Verdict(turn_kind="answer", consent="yes" if s.kind == "welcome" else "na", answers_question=True,
                        specificity="specific", knockout={"knockout_id": s.id, "status": "unclear"} if s.kind == "knockout" else None)
        if v.turn_kind == "wants_human":
            self.flag("asked for a person", "candidate asked to speak with a person instead of the AI")
            return self.force_close("wants_human")
        if v.turn_kind == "stop_request":
            self.flag("asked to stop", "candidate asked to stop or reschedule")
            return self.force_close("stop_request")
        if v.turn_kind == "off_topic":
            self.offtopic_run += 1
            if self.offtopic_run >= 3:
                self.flag("off-topic", "three off-topic turns in a row; interview closed")
                return self.force_close("off_topic")
            return Action(f"{LINE_REDIRECT} {self.short_of(self.current_question)}", "redirect")
        self.offtopic_run = 0
        if v.turn_kind == "candidate_question":
            return self._candidate_question(v)
        if v.turn_kind == "unclear":
            return self._unclear()
        return self._on_answer(v)

    def _next_prompt(self, prefix: str = "") -> Action:
        """Speak the question of the step we just entered (after _advance)."""
        s = self.step
        if self.wrapping and s.kind == "competency":
            self._skip_to_logistics()
            s = self.step
            prefix = LINE_LOGISTICS
        elif s.kind == "knockout" and self.steps[self.idx - 1].kind == "competency":
            prefix = LINE_LOGISTICS
        self.current_question = s.question
        return Action(f"{prefix} {s.question}".strip(), "question")

    def _unclear(self) -> Action:
        s = self.step
        n = self.reasks.get(s.id, 0)
        if n < 1:
            self.reasks[s.id] = n + 1
            return Action(f"{LINE_REASK} {self.short_of(self.current_question)}", "reask")
        if s.kind == "welcome":
            return self.force_close("consent_unclear")
        if s.kind == "knockout":
            return self._record_knockout(s, "unclear", "", ExtractJob(self.eval_context(), s, True, "unclear"))
        if s.kind == "competency":
            self.flag("no usable answer", f"competency {s.id}: candidate answer unclear after a re-ask")
            self._advance()
            return self._next_prompt()
        if s.kind == "candidate_questions":
            return self._close()
        self._advance()
        return self._next_prompt()

    def _permission_action(self) -> Action:
        """"Can I ask a question?": answered once with one short line, then (mid-interview) a short repeat of the pending question.
        Asked again on the same step: a shorter line; a third time: say nothing. Never a loop, never a full re-read."""
        s = self.step
        n = self.permission_asks[s.id] = self.permission_asks.get(s.id, 0) + 1
        if s.kind == "candidate_questions":
            return Action(LINE_GO_AHEAD, "answer") if n == 1 else Action("", "defer")
        if n == 1:
            return Action(f"{LINE_PERMISSION} {self.short_of(self.current_question)}", "answer")
        if n == 2:
            return Action(LINE_PERMISSION_AGAIN, "answer")
        return Action("", "defer")

    def _candidate_question(self, v: Verdict) -> Action:
        cq = v.candidate_question or {"text": "", "fact_key": "other"}
        last = self.answer_turns[-1] if self.answer_turns else ""
        if is_permission_request(cq["text"]) or is_permission_request(last):
            return self._permission_action()
        gloss = glossary_answer(cq["text"], last)  # "what is X?" about a term the posting uses: one fixed sentence, nothing invented
        if gloss:
            if self.step.kind == "candidate_questions":
                self.cq_count += 1
                if self.cq_count >= MAX_CANDIDATE_QUESTIONS:
                    self._advance_to_close_with(gloss)
                    return Action(f"{gloss} {CLOSE.format(name=self.first_name)}", "close", end=True, reason="completed")
                self.current_question = self.role.script["more_questions_prompt"]
                return Action(f"{gloss} {self.current_question}", "answer")
            return Action(f"{gloss} {self.short_of(self.current_question)}", "answer")
        fact = self.role.known_fact(cq["fact_key"])  # a spoken sentence taken from the posting, or None (not stated -> defer)
        if fact:
            answer = fact
            if cq["fact_key"] in ALWAYS_FLAG:
                self.flag("candidate asked about the client", cq["text"][:200] or "who is the client")
        else:
            answer = LINE_DEFER
            self.flag("candidate question to cover", cq["text"][:200] or "(question not captured)")
        if self.step.kind == "candidate_questions":
            self.cq_count += 1
            if self.cq_count >= MAX_CANDIDATE_QUESTIONS:
                self._advance_to_close_with(answer)
                return Action(f"{answer} {CLOSE.format(name=self.first_name)}", "close", end=True, reason="completed")
            self.current_question = self.role.script["more_questions_prompt"]
            return Action(f"{answer} {self.current_question}", "answer")
        return Action(f"{answer} {self.short_of(self.current_question)}", "answer")

    def _advance_to_close_with(self, _answer: str) -> None:
        self._advance()
        self.ended = "completed"

    def _on_answer(self, v: Verdict) -> Action:
        s = self.step
        if s.kind == "welcome":
            if v.consent == "yes":
                self._advance()
                return self._next_prompt()
            if v.consent == "no":
                self.flag("declined AI interview", "candidate did not agree to start; interview ended")
                return self.force_close("declined")
            return self._unclear()
        if s.kind in ("rapport", "background"):
            job = ExtractJob(self.eval_context(), s, True) if s.kind == "background" else None  # motivation is not competency evidence
            self._advance()
            a = self._next_prompt()
            a.extract = job
            return a
        if s.kind == "competency":
            specific = v.specificity == "specific" and v.answers_question
            if not specific and s.id not in self.probe_used:  # probe EXACTLY once
                self.probe_used.add(s.id)
                self.post_probe = True
                self.current_question = self.comp(s.id).probe_if_vague
                return Action(self.current_question, "probe")
            job = ExtractJob(self.eval_context(), s, specific)
            self._advance()
            a = self._next_prompt()
            a.extract = job
            return a
        if s.kind == "knockout":
            ko = v.knockout if v.knockout and v.knockout["knockout_id"] == s.id else None
            if ko is None or ko["status"] == "unclear":
                n = self.reasks.get(s.id, 0)
                if n < 1:
                    self.reasks[s.id] = n + 1
                    return Action(f"{LINE_REASK_KO} {s.question}", "reask")
                return self._record_knockout(s, "unclear", "", ExtractJob(self.eval_context(), s, True, "unclear"))
            return self._record_knockout(s, ko["status"], "", ExtractJob(self.eval_context(), s, True, ko["status"]))
        if s.kind == "candidate_questions":  # "no, that's all"
            return self._close()
        return self._close()

    def _record_knockout(self, s: Step, status: str, answer: str, job: ExtractJob) -> Action:
        self.knockouts[s.id] = {"status": status, "answer": answer, "quote": "", "quote_verified": False, "t": round(self.elapsed(), 1)}
        self._advance()
        a = self._next_prompt()
        a.extract = job
        return a

    # ---- background extraction result ----------------------------------------------------------------------------------
    def apply_extraction(self, job: ExtractJob, x: Extraction) -> None:
        """Validate and store what the (slow, off-path) extraction returned. Never raises."""
        turns = job.ctx.answer_turns
        docs = list(turns) + [" ".join(turns[i:i + 2]) for i in range(len(turns) - 1)]  # a quote may span two adjacent fragments
        t = round(self.elapsed(), 1)
        if x.protected_info_volunteered:
            self.protected_volunteered += 1  # content never stored
        if x.failed:
            self.flag("evidence extraction failed", f"step {job.step.id}: {x.failed[:120]}")
        stored_for_step = False
        unclear = False
        for e in x.evidence:
            flagged = mentions_protected(e["quote"]) or mentions_protected(e["note"])
            if flagged and x.protected_info_volunteered:  # genuinely volunteered: nothing from this quote reaches the record
                self.rejected_quotes.append({"t": t, "step": job.step.id, "quote": "(withheld)", "reason": "protected ground"})
                continue
            m = validate_quote(e["quote"], docs)
            if not m.ok:
                self.rejected_quotes.append({"t": t, "step": job.step.id, "competency_id": e["competency_id"],
                                             "quote": e["quote"], "score": round(m.score, 3), "reason": m.reason})
                continue
            if flagged:  # not volunteered: most likely an ASR mishear ("skid" heard as "last kid"). Redact only the span, keep the rating.
                e = {**e, "note": redact_protected(e["note"])[0]}
                m.text = redact_protected(m.text)[0]
                unclear = True
            forced = job.step.kind == "competency" and e["competency_id"] == job.step.id and not job.final_specific
            self.evidence.append({"t": t, "step": job.step.id, "competency_id": e["competency_id"], "quote": m.text,
                                  "assessment": "weak" if forced else e["assessment"],
                                  "assessment_forced_weak": forced, "note": e["note"], "match_score": round(m.score, 3)})
            stored_for_step |= e["competency_id"] == job.step.id
        if unclear:
            self.flag("transcript unclear", f"verify this answer ({job.step.id})")
        if job.step.kind == "competency" and not stored_for_step:
            # Always leave a record for a competency we asked about. Fallback quote = the candidate's own first words (verbatim by
            # construction), logged weak if the answer stayed vague, otherwise "unrated" and flagged.
            base = max(turns, key=len, default="") if turns else ""
            words = redact_protected(" ".join(base.split()[:30]))[0].split()
            if words:
                weak = not job.final_specific
                self.evidence.append({"t": t, "step": job.step.id, "competency_id": job.step.id, "quote": " ".join(words),
                                      "assessment": "weak" if weak else "unrated", "assessment_forced_weak": weak,
                                      "note": "fallback: no verifiable quote from the evaluator", "match_score": 1.0})
                if not weak:
                    self.flag("evidence unrated", f"competency {job.step.id}: no verifiable quote was extracted")
        if job.step.kind == "knockout" and job.step.id in self.knockouts:
            rec = self.knockouts[job.step.id]
            rec["answer"] = redact_protected(x.knockout_answer)[0] if x.knockout_answer else rec["answer"]
            if x.knockout_quote:
                m = validate_quote(x.knockout_quote, docs)
                rec["quote_verified"] = m.ok
                rec["quote"] = redact_protected(m.text)[0] if m.ok else ""
                if not m.ok:
                    self.rejected_quotes.append({"t": t, "step": job.step.id, "quote": x.knockout_quote, "score": round(m.score, 3),
                                                 "reason": m.reason})
            if rec["status"] in ("pass", "fail") and not rec["quote_verified"]:
                self.flag("verify knock-out", f"{job.step.id}: answer could not be verified against the transcript")

    # ---- audit ---------------------------------------------------------------------------------------------------------
    def summary(self) -> dict:
        return {"role": self.role.id, "ended": self.ended, "stage_log": self.stage_log, "evidence": self.evidence,
                "knockouts": self.knockouts, "flags": self.flags, "rejected_quotes": self.rejected_quotes,
                "probe_used": sorted(self.probe_used), "protected_info_volunteered_count": self.protected_volunteered,
                "placeholders": self.role.placeholders, "elapsed_s": round(self.elapsed(), 1)}
