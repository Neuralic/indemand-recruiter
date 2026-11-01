"""Spike-only copy of the §5 tools and a condensed §6/§7 prompt for role #5905.

NOT product code - Phase 1 will build the real thing. It exists so Phase 0 tests function
calling against the actual tool surface the brief specifies, with enum-constrained arguments.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

STAGES = ["welcome", "rapport", "background", "competencies", "logistics", "candidate_questions", "close"]
COMPETENCIES = ["tech_leadership", "reliability", "safety_compliance", "team_leadership", "project_delivery"]
KNOCKOUTS = ["work_auth", "licence", "location", "salary", "notice_period"]

DOMAIN_TERMS = [
    "APEGA", "P.Eng", "CEC", "arc flash", "MCC", "switchgear", "PLC", "SCADA", "LOTO",
    "CMMS", "SAP PM", "Maximo", "RCM", "MTBF",
]

ROLE_FACTS = """Role: Electrical Engineering Supervisor, Edmonton, AB, Canada (#5905).
Known facts for candidate questions: NONE beyond the title and location. Reporting line, schedule,
compensation and benefits are UNKNOWN - never invent them; say it's a great one for Stefanie and flag it."""

SYSTEM_PROMPT = f"""You are Avery, IN DEMAND Recruitment's AI screening assistant, interviewing a candidate by voice.
You sound like someone with 20 years in heavy industry: warm Canadian-professional, conversational,
1-3 short sentences per turn, never over-praise, never repeat a question already answered.

{ROLE_FACTS}

STAGES, in order: welcome -> rapport -> background -> competencies -> logistics -> candidate_questions -> close.
You MUST report progress and findings by calling tools. Tools are how the recruiter sees the interview.
- advance_stage(stage): call whenever you move to the next stage. Never skip logistics before close.
- log_evidence(competency_id, quote, assessment, note): call after EVERY substantive answer to a competency
  question - including vague ones (assess them 'weak'). quote = the candidate's own words.
- record_knockout(knockout_id, answer, status): call for EACH logistics answer (work authorization, licence,
  location, salary, notice period) as soon as it is given.
- flag_for_recruiter(topic, reason): call when you cannot answer a candidate question from the role facts above,
  or the candidate asks for a human.
- end_interview(reason): call ONLY after you have said goodbye in the close stage.

FLOW
1. welcome: say hi, introduce yourself as an AI assistant, say the call is recorded for Stefanie's team, a human
   reviews every interview and no decision is made by AI, they can ask for a person instead; ask to start.
   When they agree, call advance_stage('rapport').
2. rapport: why open to a move now. Then advance_stage('background').
3. background: one specific question on their experience (never 'walk me through your resume'). Then
   advance_stage('competencies').
4. competencies ({', '.join(COMPETENCIES)}): one primary question each. If an answer is vague, probe ONCE for a
   specific example (situation, what THEY did, a number). Call log_evidence after each answer.
5. logistics: ask work eligibility ('Are you legally eligible to work in Canada?'), APEGA P.Eng registration,
   location/relocation, salary expectations, notice period - one at a time, record_knockout for each.
6. candidate_questions: answer only from the role facts; otherwise 'That's a great one for Stefanie, I'll make
   sure she covers it' and flag_for_recruiter.
7. close: next steps (Stefanie's team reviews every interview within one business day), thank them, then end_interview.

STYLE RULES: Never mention tools, logging, notes or "recording that" to the candidate - call tools silently and keep
talking naturally. Ask ONE question per turn. Do not skip or reorder stages.

GUARDRAILS: never ask about or react to age, race, religion, gender, family or marital status, disability,
pregnancy, origin or similar. Never invent facts about the company, pay or benefits. Stay on topic.
"""


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> FunctionSchema:
    return FunctionSchema(name=name, description=description, properties=properties, required=required)


TOOLS = ToolsSchema(
    standard_tools=[
        _fn("advance_stage", "Move the interview to the next stage.",
            {"stage": {"type": "string", "enum": STAGES}}, ["stage"]),
        _fn("log_evidence", "Log evidence for a competency after a substantive answer.",
            {
                "competency_id": {"type": "string", "enum": COMPETENCIES},
                "quote": {"type": "string", "description": "Candidate's own words, verbatim."},
                "assessment": {"type": "string", "enum": ["strong", "adequate", "weak"]},
                "note": {"type": "string"},
            }, ["competency_id", "quote", "assessment", "note"]),
        _fn("record_knockout", "Record a logistics / knock-out answer.",
            {
                "knockout_id": {"type": "string", "enum": KNOCKOUTS},
                "answer": {"type": "string"},
                "status": {"type": "string", "enum": ["pass", "fail", "unclear"]},
            }, ["knockout_id", "answer", "status"]),
        _fn("flag_for_recruiter", "Flag a topic for Stefanie to verify on her call.",
            {"topic": {"type": "string"}, "reason": {"type": "string"}}, ["topic", "reason"]),
        _fn("end_interview", "End the interview after the closing has been spoken.",
            {"reason": {"type": "string"}}, ["reason"]),
    ]
)


@dataclass
class ToolCallRecord:
    t: float
    turn: int
    name: str
    args: dict[str, Any]
    ok: bool
    note: str = ""


@dataclass
class ToolRecorder:
    calls: list[ToolCallRecord] = field(default_factory=list)
    stage: str = "welcome"
    turn: int = 0  # set by the test driver
    t0: float = field(default_factory=time.time)

    def _validate(self, name: str, a: dict[str, Any]) -> tuple[bool, str]:
        if name == "advance_stage":
            s = a.get("stage")
            if s not in STAGES:
                return False, f"bad stage {s!r}"
            # §5 stage gate: cannot reach close before logistics is done
            if s == "close" and STAGES.index(self.stage) < STAGES.index("logistics"):
                return False, "STAGE GATE: close before logistics"
            if STAGES.index(s) < STAGES.index(self.stage):
                return False, f"cannot go backwards {self.stage}->{s}"
        enums = {
            "log_evidence": {"competency_id": COMPETENCIES, "assessment": ["strong", "adequate", "weak"]},
            "record_knockout": {"knockout_id": KNOCKOUTS, "status": ["pass", "fail", "unclear"]},
        }.get(name, {})
        for k, allowed in enums.items():
            if a.get(k) not in allowed:
                return False, f"{k}={a.get(k)!r} not in enum"
        return True, ""

    def handler(self, name: str):
        async def _h(params: FunctionCallParams):
            ok, note = self._validate(name, params.arguments)
            self.calls.append(ToolCallRecord(time.time() - self.t0, self.turn, name, dict(params.arguments), ok, note))
            if name == "advance_stage" and ok:
                self.stage = params.arguments["stage"]
            if ok:
                await params.result_callback({"status": "ok", "stage": self.stage})
            else:
                await params.result_callback({"status": "error", "error": note})
        return _h

    def register(self, llm, async_tools: bool = False) -> None:
        for fn in TOOLS.standard_tools:
            if async_tools:
                # cancel_on_interruption=False => "async" call: the model keeps talking without waiting for the result
                llm.register_function(fn.name, self.handler(fn.name), cancel_on_interruption=False)
            else:
                llm.register_function(fn.name, self.handler(fn.name))
