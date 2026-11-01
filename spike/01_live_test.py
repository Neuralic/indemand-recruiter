"""Phase 0 / item 1: does OpenAIRealtimeLLMService accept gpt-realtime-2.1, end to end over SmallWebRTC?

One-minute live conversation: bot greets, synthetic candidate answers twice, bot replies.
Pass criteria (checked against the log, not by ear):
  - session.updated confirms the model string we asked for
  - no 'error' event
  - bot audio received over WebRTC after connect (greeting latency measured)
  - the candidate's speech is transcribed and the bot's reply is on-topic
"""

import asyncio
import json
import sys
import time

from loguru import logger

import harness as h
from pipecat.frames.frames import LLMRunFrame
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.processors.aggregators.llm_context import LLMContext

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gpt-realtime-2.1"
log_path = h.attach_log_file(f"01_live_test_{MODEL}")


class LoggingRealtime(OpenAIRealtimeLLMService):
    async def _handle_evt_session_created(self, evt):
        s = evt.session
        logger.info(f"SERVER session.created model={s.model!r}")
        await super()._handle_evt_session_created(evt)

    async def _handle_evt_session_updated(self, evt):
        s = evt.session
        logger.info(f"SERVER session.updated model={s.model!r} reasoning={s.reasoning!r} "
                    f"voice={(s.audio.output.voice if s.audio and s.audio.output else None)!r}")
        await super()._handle_evt_session_updated(evt)

    async def _handle_evt_error(self, evt):
        logger.error(f"SERVER ERROR: {evt.error.model_dump() if hasattr(evt.error, 'model_dump') else evt.error}")
        await super()._handle_evt_error(evt)

    async def _handle_evt_response_done(self, evt):
        u = evt.response.usage
        logger.info(f"SERVER response.done status={evt.response.status!r} usage="
                    f"{u.model_dump() if hasattr(u, 'model_dump') else u}")
        await super()._handle_evt_response_done(evt)


async def main() -> int:
    llm = LoggingRealtime(
        api_key=h.api_key(),
        settings=OpenAIRealtimeLLMService.Settings(
            model=MODEL,
            system_instruction=(
                "You are Avery, a warm Canadian-professional voice screening assistant for IN DEMAND "
                "Recruitment. Speak in 1-2 short sentences. Greet the candidate, say you are an AI assistant, "
                "then ask one question at a time about their maintenance leadership experience."
            ),
            session_properties=events.SessionProperties(
                audio=events.AudioConfiguration(
                    input=events.AudioInput(
                        transcription=events.InputAudioTranscription(),
                        turn_detection=events.SemanticTurnDetection(),
                    ),
                ),
            ),
        ),
    )
    context = LLMContext([{"role": "developer", "content": "Greet the candidate, Dana, and begin."}])
    transcript: list[str] = []

    async def on_user_msg(agg, message):
        transcript.append(f"USER: {message.content}")
        logger.info(f"TRANSCRIPT USER: {message.content}")

    async def on_asst(agg, message):
        transcript.append(f"BOT : {message.content}")
        logger.info(f"TRANSCRIPT BOT : {message.content}")

    sess, run_task, runner = await h.open_session(
        llm, context,
        user_aggregator_handlers={"on_user_turn_message_added": on_user_msg},
        assistant_handlers={"on_assistant_turn_stopped": on_asst},
    )
    t0 = time.time()
    await asyncio.wait_for(sess.connected.wait(), 30)
    t_conn = time.time()
    logger.info(f"client connected after {t_conn - t0:.2f}s")
    await sess.worker.queue_frames([LLMRunFrame()])
    t_run = time.time()

    first, last = await h.wait_bot_turn(sess.candidate, timeout=30)
    assert first, "no bot audio received for the greeting"
    logger.info(f"GREETING first audio {first - t_run:.2f}s after LLMRunFrame; "
                f"{first - t_conn:.2f}s after connect; duration {last - first:.1f}s")

    for line in [
        "Hi, I'm Dana. Yeah, I'm ready to get started.",
        "I've been a maintenance manager at a gas plant outside Edmonton for six years, "
        "running a team of about twenty technicians.",
    ]:
        dur = sess.candidate.say(line, lead_silence_s=0.3)
        t_say = time.time()
        await asyncio.sleep(dur + 0.4)
        first, last = await h.wait_bot_turn(sess.candidate, timeout=30)
        assert first, f"bot did not respond to: {line}"
        logger.info(f"REPLY latency {first - (t_say + dur):.2f}s after candidate finished; reply {last - first:.1f}s")

    h.write_wav(h.OUT / f"01_bot_audio_{MODEL}.wav", bytes(sess.candidate.bot.pcm), sess.candidate.bot.rate, sess.candidate.bot.channels)
    await h.close_session(sess, run_task, runner)
    print("\n".join(transcript))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
