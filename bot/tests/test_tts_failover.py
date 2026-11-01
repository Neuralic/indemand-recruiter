import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_failover import FailoverCore, StubProvider, split_sentences  # noqa: E402

TEXT = "Hello there my friend."


def prov(name, faults="", ttfb=0.01, first=0.3):
    p = StubProvider(name, ttfb=ttfb, faults=faults, stall_s=first + 0.4)
    p.first_byte_timeout = first
    p.chunk_timeout = first
    return p


async def run(core, sentences):
    audio, results = [], []

    async def emit(chunk):
        audio.append((core.active.name, len(chunk)))

    for s in sentences:
        results.append(await core.speak_sentence(s, emit))
    return audio, results


def go(core, sentences):
    return asyncio.run(run(core, sentences))


def switches(core):
    return [e for e in core.log if e["kind"] == "switch"]


def test_no_fault_stays_on_primary():
    core = FailoverCore([prov("cartesia"), prov("fish")])
    audio, results = go(core, [TEXT] * 3)
    assert results == [True] * 3 and core.idx == 0 and not switches(core)
    assert {a[0] for a in audio} == {"cartesia"}


def test_402_before_audio_respeaks_same_sentence_on_fallback_and_stays_there():
    core = FailoverCore([prov("cartesia", "402@1"), prov("fish")])
    audio, results = go(core, ["First sentence here.", "Second sentence here.", "Third sentence here."])
    assert results == [True, True, True] and core.idx == 1
    sw = switches(core)
    assert len(sw) == 1 and sw[0]["reason"] == "quota" and not sw[0]["mid_sentence"] and sw[0]["to"] == "fish"
    assert core.providers[0].spoken == ["First sentence here."]
    assert core.providers[1].spoken == ["Second sentence here.", "Third sentence here."]  # the failed sentence was NOT lost, nor repeated


def test_a_single_stall_is_retried_on_a_fresh_connection_and_does_not_switch():
    core = FailoverCore([prov("cartesia", "stall@0"), prov("fish")])
    audio, results = go(core, ["Only one sentence."])
    assert results == [True] and core.idx == 0 and not switches(core) and not core.failed
    assert core.providers[0].resets == 1 and core.providers[0].spoken == ["Only one sentence."]
    assert [e["kind"] for e in core.log] == ["retry"] and core.log[0]["reason"] == "stall"
    assert all(name == "cartesia" for name, _ in audio)  # nothing from the stalled attempt leaked into the output


def test_stall_twice_in_a_row_switches_and_respeaks_the_sentence_on_the_fallback():
    core = FailoverCore([prov("cartesia", "stall@0,stall@1"), prov("fish")])
    audio, results = go(core, ["Only one sentence."])
    assert results == [True] and core.idx == 1
    sw = switches(core)
    assert len(sw) == 1 and sw[0]["reason"] == "stall" and not sw[0]["mid_sentence"]
    assert [e["kind"] for e in core.log] == ["retry", "switch"]
    assert core.providers[1].spoken == ["Only one sentence."] and core.providers[0].spoken == []
    assert all(name == "fish" for name, _ in audio)


def test_quota_is_never_retried():
    core = FailoverCore([prov("cartesia", "402@0"), prov("fish")])
    go(core, ["One."])
    assert core.providers[0].resets == 0 and [e["kind"] for e in core.log] == ["switch"] and core.idx == 1


def test_one_cartesia_stall_plus_one_fish_timeout_does_not_end_the_call():
    # the live-session failure: Cartesia stalled once and Fish then timed out once. Each provider's retry absorbs a single blip.
    core = FailoverCore([prov("cartesia", "stall@0,stall@1"), prov("fish", "error@0")])
    audio, results = go(core, ["Stefanie will confirm that with you."])
    assert results == [True] and not core.failed and core.idx == 1
    assert [e["kind"] for e in core.log] == ["retry", "switch", "retry"]
    assert core.providers[1].spoken == ["Stefanie will confirm that with you."] and core.providers[1].resets == 1


def test_disconnect_mid_sentence_is_not_repeated_next_sentence_uses_fallback():
    core = FailoverCore([prov("cartesia", "disconnect@0"), prov("fish")])  # mid-sentence failures are not retried: audio was already heard
    audio, results = go(core, ["A fairly long first sentence that gets cut off midway.", "Second sentence."])
    assert results == [True, True] and core.idx == 1
    sw = switches(core)
    assert len(sw) == 1 and sw[0]["reason"] == "disconnect" and sw[0]["mid_sentence"]
    assert core.providers[1].spoken == ["Second sentence."]  # the truncated sentence was NOT restarted on the fallback
    assert [n for n, _ in audio if n == "cartesia"] and [n for n, _ in audio if n == "fish"]


def test_both_providers_failing_reports_failure_and_stops():
    core = FailoverCore([prov("cartesia", "402@0"), prov("fish", "error@0,error@1")])
    audio, results = go(core, ["Anything at all."])
    assert results == [False] and core.failed and not audio
    assert [e["kind"] for e in core.log] == ["switch", "retry", "all_providers_failed"]


def test_sticky_no_switching_back():
    core = FailoverCore([prov("cartesia", "402@0"), prov("fish")])
    go(core, ["One.", "Two.", "Three.", "Four."])
    assert core.idx == 1 and len(switches(core)) == 1 and core.providers[0].n == 1  # Cartesia was never tried again


def test_split_sentences():
    assert split_sentences("Hi David, I'm Avery. Shall we start? Great!") == ["Hi David, I'm Avery.", "Shall we start?", "Great!"]
    assert split_sentences("One line with no end") == ["One line with no end"]


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
