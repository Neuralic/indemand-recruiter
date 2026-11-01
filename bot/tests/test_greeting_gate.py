"""The greeting/disclosure cannot be skipped: the mic is silence until the last greeting sentence has played (and 1.5 s after connect)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline_parts import GreetingGate  # noqa: E402

FRAME = b"\x10\x27" * 480  # 20 ms of loud audio at 24 kHz
SILENCE = bytes(len(FRAME))


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def make():
    c, opened = Clock(), []
    return GreetingGate(on_open=lambda: opened.append(c.t), clock=c), c, opened


def feed(g, c, until):
    """20 ms mic frames up to `until` seconds after the first one; returns the audio the realtime service received."""
    out = []
    while c.t - 100.0 < until:
        out.append(g.mic(FRAME))
        c.t += 0.02
    return out


def test_a_click_1_2_s_after_connect_never_reaches_the_realtime_service():
    g, c, opened = make()
    got = feed(g, c, 1.2)
    assert all(f == SILENCE for f in got)
    g.mic(FRAME)  # the transient itself, at t=1.2 s
    g.tts_audio(48000 * 2, 24000)  # even a very short greeting that has already finished ...
    g.tts_stopped()
    c.t += 0.1
    assert g.mic(FRAME) == SILENCE and not opened  # ... is not heard-through before 1.5 s after connect AND the playback tail


def test_speech_during_the_greeting_is_silence_and_nothing_opens_until_the_last_sentence_has_played():
    g, c, opened = make()
    g.tts_audio(24000 * 2 * 3, 24000)  # 3 s of audio arrive at once (synthesis is faster than real time)
    c.t += 0.5
    g.tts_audio(24000 * 2 * 22, 24000)  # 25 s in total
    g.tts_stopped()  # TTS finished at t=0.5 s, but the audio plays until t=25 s
    got = feed(g, c, 20.0)  # the candidate talks over the greeting
    assert all(f == SILENCE for f in got) and not opened
    got = feed(g, c, 25.2)
    assert all(f == SILENCE for f in got) and not opened  # not yet: 25 s + the tail
    got = feed(g, c, 26.0)
    assert opened and got[-1] == FRAME  # open now: the candidate's words are heard from here on, nothing was repeated or replayed
    assert len(opened) == 1 and 100 + 25.2 < opened[0] < 100 + 25.6


def test_failsafe_opens_the_mic_if_the_tts_never_reports():
    g, c, opened = make()
    feed(g, c, 59.0)
    assert not opened
    feed(g, c, 61.0)
    assert opened


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
