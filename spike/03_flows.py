"""Phase 0 / item 3: do Pipecat Flows work with OpenAIRealtimeLLMService?

Two-node flow. Node 2's task_messages contain a MARKER the bot would only say if the node's task message really
reached the model. Node 2 also exposes a tool (log_note) that node 1 does not have.

We check three separate Flows mechanisms, because they travel as different frames:
  (a) role_message      -> LLMUpdateSettingsFrame  (system instruction)
  (b) task_messages     -> LLMMessagesAppendFrame  (APPEND strategy)  <- Realtime service has a stub for this
  (c) functions (tools) -> LLMSetToolsFrame
  (d) transition via a function's returned next node
"""

import asyncio
import json
import sys
import time

from loguru import logger

import harness as h
from pipecat.flows import FlowManager, FlowsFunctionSchema, NodeConfig
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

STRATEGY = sys.argv[1] if len(sys.argv) > 1 else "append"  # append | reset
NAME = f"03_flows_{STRATEGY}"
h.attach_log_file(NAME)
MARKER = "banana protocol engaged"
ROLE_MARK = "Always end every reply with the word 'over'."

observed = {"proceed_called": False, "log_note_called": False, "node2_entered": False}


def node2() -> NodeConfig:
    async def log_note(args, flow_manager):
        observed["log_note_called"] = True
        logger.info(f"FLOWS log_note called args={args}")
        return {"status": "ok"}, None

    observed["node2_entered"] = True
    return {
        "name": "node2",
        "task_messages": [{
            "role": "developer",
            "content": f"You are now in node 2. Begin your reply with exactly the words '{MARKER.title()}.' "
                       "Then ask ONE question about the candidate's maintenance experience. After every answer "
                       "call log_note with a short note.",
        }],
        "functions": [FlowsFunctionSchema(
            name="log_note", description="Log a note about the candidate's answer.",
            properties={"note": {"type": "string"}}, required=["note"], handler=log_note)],
    }


def node1() -> NodeConfig:
    async def proceed(args, flow_manager):
        observed["proceed_called"] = True
        logger.info("FLOWS proceed called -> transitioning to node2")
        return {"status": "ok"}, node2()

    return {
        "name": "node1",
        "role_message": f"You are Avery, a concise voice screening assistant for IN DEMAND. {ROLE_MARK}",
        "task_messages": [{
            "role": "developer",
            "content": "Greet the candidate Dana in one sentence and ask if she is ready to begin. "
                       "When she says yes, call proceed.",
        }],
        "functions": [FlowsFunctionSchema(
            name="proceed", description="Call when the candidate agrees to begin.",
            properties={}, required=[], handler=proceed)],
    }


async def main() -> int:
    llm = OpenAIRealtimeLLMService(
        api_key=h.api_key(),
        settings=OpenAIRealtimeLLMService.Settings(
            model="gpt-realtime-2.1",
            session_properties=events.SessionProperties(audio=events.AudioConfiguration(input=events.AudioInput(
                transcription=events.InputAudioTranscription(),
                turn_detection=events.SemanticTurnDetection(),
            ))),
        ),
    )
    context = LLMContext([])
    bot_lines: list[str] = []

    async def on_asst(agg, m):
        if m.content and m.content.strip():
            bot_lines.append(m.content.strip())
            logger.info(f"BOT: {m.content}")

    for line in ("Yes, I'm ready.", "I've been a maintenance manager at a gas plant for six years."):
        h.synth(line)
    sess, run_task, runner = await h.open_session(llm, context, assistant_handlers={"on_assistant_turn_stopped": on_asst})
    await asyncio.wait_for(sess.connected.wait(), 30)

    from pipecat.flows import ContextStrategy, ContextStrategyConfig
    strat = ContextStrategyConfig(strategy=ContextStrategy.RESET if STRATEGY == "reset" else ContextStrategy.APPEND)
    fm = FlowManager(llm=llm, context_aggregator=sess.aggregators, worker=sess.worker, context_strategy=strat)
    await fm.initialize(node1())
    first, last = await h.wait_bot_turn(sess.candidate, timeout=40, quiet_s=2.5)
    logger.info(f"node1 greeting heard: {bool(first)}")

    for line in ("Yes, I'm ready.", "I've been a maintenance manager at a gas plant for six years."):
        dur = await sess.candidate.asay(line, lead_silence_s=0.3)
        await asyncio.sleep(dur + 0.5)
        await h.wait_bot_turn(sess.candidate, timeout=40, quiet_s=2.5)
        await asyncio.sleep(1.5)

    await h.close_session(sess, run_task, runner)
    said_marker = any(MARKER in b.lower() for b in bot_lines)
    role_applied = sum(b.lower().rstrip(" .!?\"'").endswith("over") for b in bot_lines)
    res = {
        "strategy": STRATEGY, "bot_lines": bot_lines, **observed,
        "(a) role_message applied (bot lines ending in 'over')": f"{role_applied}/{len(bot_lines)}",
        "(b) node2 task_message reached model (marker said)": said_marker,
        "(c) node2 tool available + called": observed["log_note_called"],
        "(d) transition via function result": observed["proceed_called"] and observed["node2_entered"],
    }
    (h.OUT / f"{NAME}.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
