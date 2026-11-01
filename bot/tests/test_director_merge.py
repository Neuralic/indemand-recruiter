"""Director merge-window behaviour with a fake evaluator and no Pipecat pipeline (frames are captured by replacing push_frame)."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluator import Verdict  # noqa: E402
from interview import Interview  # noqa: E402
from pipeline_parts import Director  # noqa: E402
from role_config import load_role  # noqa: E402

ROLE = load_role("5905")


class FakeEval:
    def __init__(self, verdicts, delay=0.05):
        self.verdicts, self.delay, self.seen = list(verdicts), delay, []

    async def classify(self, ctx):
        self.seen.append(list(ctx.answer_turns))
        await asyncio.sleep(self.delay)
        v = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        v.latency_s = self.delay
        return v

    async def extract(self, ctx):
        from evaluator import Extraction
        return Extraction()


class FakeBrain:
    async def note_bot_line(self, text):
        pass


def make(verdicts):
    iv = Interview(ROLE, "David")
    iv.opening()
    iv.on_user_turn("Yeah")
    iv.decide(Verdict(turn_kind="answer", consent="yes"))  # now on rapport; one question was asked
    iv.answer_turns, iv._answer_hist = [], []
    log, spoken = [], []
    d = Director(iv, FakeEval(verdicts), FakeBrain(), log, {})
    d.MIN_APPLY_S, d.LATE_MERGE_S, d.CONTINUE_S, d.QUIET_BEFORE_REASK_S = 0.4, 0.8, 0.3, 0.3  # scaled down for the test
    d.ack_timeout = 0.05

    async def capture(frame, direction=None):
        spoken.append(getattr(frame, "text", None))

    d.push_frame = capture
    d.responses_ended = 1  # no acknowledgement to wait for
    return d, iv, log, spoken


def say(d, text, talking_after=False):
    d.last_stop_wall = time.time()
    d.user_talking = False
    d.on_user_turn(text)


def kinds(log, k):
    return [e for e in log if e["kind"] == k]


def run(coro):
    async def wrapper():
        await coro()
        await asyncio.sleep(0)
    asyncio.run(wrapper())


def test_resuming_within_the_window_merges_into_one_answer():
    async def go():
        d, iv, log, spoken = make([Verdict(answers_question=True, specificity="specific")])
        say(d, "At a gas plant I designed the three-phase power for a new compressor building.")
        await asyncio.sleep(0.15)  # a 2 s thinking pause, scaled: the second half arrives before anything is acted on
        say(d, "It had an MCC and VFDs and I specified the grounding and bonding.")
        await asyncio.sleep(1.2)
        assert len(kinds(log, "action")) == 1, [e.get("text") for e in kinds(log, "action")]
        assert d.ev.seen[-1] == ["At a gas plant I designed the three-phase power for a new compressor building.",
                                 "It had an MCC and VFDs and I specified the grounding and bonding."]
        assert kinds(log, "verdict_superseded")
    run(go)


def test_incomplete_fragment_waits_and_is_merged_with_what_follows():
    async def go():
        d, iv, log, spoken = make([Verdict(turn_kind="unclear", answers_question=False), Verdict(answers_question=True)])
        say(d, "All right, yeah so")
        await asyncio.sleep(0.25)
        say(d, "I'm an electrical supervisor at an industrial plant, leading six people.")
        await asyncio.sleep(1.5)
        acts = kinds(log, "action")
        assert len(acts) == 1 and acts[0]["action_kind"] != "reask", [(e["action_kind"], e.get("text")) for e in acts]
        assert kinds(log, "turn")[0]["incomplete"] is True and kinds(log, "turn")[1]["merged_with"] == 1
    run(go)


def test_a_fragment_is_never_re_asked():
    async def go():
        d, iv, log, spoken = make([Verdict(turn_kind="unclear", answers_question=False)])
        say(d, "First of all,")
        await asyncio.sleep(1.6)
        assert not kinds(log, "action") and kinds(log, "fragment_no_reask")
        assert not any("catch" in (t or "") for t in spoken)
        assert d._silence_task is not None  # the silence ladder is running instead
        d.shutdown()
    run(go)


def test_a_finished_turn_is_acted_on_once_the_window_has_passed():
    async def go():
        d, iv, log, spoken = make([Verdict(answers_question=True, specificity="specific")])
        say(d, "My plant is being sold, so I want a bigger team to lead.")
        await asyncio.sleep(1.0)
        acts = kinds(log, "action")
        assert len(acts) == 1 and acts[0]["action_kind"] == "question"
        waited = kinds(log, "continuation_wait")
        assert not waited or waited[0]["incomplete"] is False
        d.shutdown()
    run(go)


def test_a_garbled_complete_turn_is_re_asked_only_after_the_candidate_has_been_quiet():
    async def go():
        d, iv, log, spoken = make([Verdict(turn_kind="unclear", answers_question=False)])
        say(d, "Panazzing question.")
        t0 = time.time()
        await asyncio.sleep(1.4)
        acts = kinds(log, "action")
        assert len(acts) == 1 and acts[0]["action_kind"] == "reask"
        assert acts[0]["t"] - d.last_stop_wall >= d.QUIET_BEFORE_REASK_S - 0.02
        d.shutdown()
    run(go)


def test_ack_is_dropped_if_the_candidate_resumed():
    async def go():
        d, iv, log, spoken = make([Verdict()])
        d.user_talking = True
        d.ack_buf = "Thanks, noted."
        await d._ack_flush()
        assert kinds(log, "ack_dropped") and not spoken
        d.user_talking = False
        d.ack_buf = "Thanks, noted."
        await d._ack_flush()
        assert kinds(log, "ack") and spoken == ["Thanks, noted."]
    run(go)


def test_director_varies_acknowledgements_and_budgets_thank_you():
    async def go():
        d, iv, log, spoken = make([Verdict()])
        for raw in ["Thanks, that's helpful.", "Okay, thanks for sharing.", "Thanks, noted.", "NONE", "Got it, three-phase power."]:
            d.ack_buf = raw
            await d._ack_flush()
        said = [e["text"] for e in kinds(log, "ack")]
        assert sum("thank" in t.lower() for t in said) == 1 and said[0] == "Thanks, that's helpful."
        assert kinds(log, "ack_skipped") and len(spoken) == len(said) == 4  # the NONE spoke nothing
    run(go)


def test_consent_before_the_disclosure_has_played_is_ignored_and_nothing_is_repeated():
    async def go():
        d, iv, log, spoken = make([Verdict(turn_kind="answer", consent="yes", answers_question=True)])
        iv2 = Interview(ROLE, "David")
        iv2.opening()
        d.iv = iv2
        d.greeting_done = False  # Director.start() has begun the greeting
        say(d, "Yes, sure.")
        await asyncio.sleep(0.9)
        assert kinds(log, "turn_ignored_greeting") and not kinds(log, "turn") and not kinds(log, "action") and not spoken
        assert iv2.stage == "welcome" and not iv2.answer_turns
        d.greeting_played()  # the full disclosure, ending in "Shall we start?", has been heard
        say(d, "Yes, go ahead.")
        await asyncio.sleep(1.2)
        acts = kinds(log, "action")
        assert len(acts) == 1 and acts[0]["action_kind"] == "question" and iv2.stage == "rapport"
        d.shutdown()
    run(go)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
